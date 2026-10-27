"""Find new physical roles in the validated, transient entity binding index.

Equal keys are retrieval evidence only. This module emits no identity decision,
stores no source values, and performs no additional source query.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import json

from lightgraph.contracts import RuleSpec, SourceRef, canonical


@dataclass(frozen=True)
class RoleOccurrence:
    source: SourceRef
    rule_ids: tuple[str, ...]
    column_refs: tuple[str, ...]
    count: int
    code: str = 'unexplained_role_occurrence'


def _role(rule, key):
    """Extract a comparable key without conflating tenant or positional scopes."""
    namespaces = list(rule.scope.namespaces)
    if rule.kind == 'wide_hierarchy':
        # Other policies already resolve within a verified domain or validate
        # repeated fills. Their binding index has no per-occurrence position.
        if rule.identity.level_policy != 'level_qualified' or len(key) != len(namespaces) + 2:
            return None
        level = next((item for item in rule.hierarchy.levels if item.level == key[-2]), None)
        if level is None:
            return None
        return namespaces, key[:len(namespaces)], [level.code], [key[-1]]
    if rule.kind not in ('column_group_entity', 'long_hierarchy'):
        return None
    columns = list(rule.identity.key_columns)
    if len(key) != len(columns) or not set(namespaces) <= set(columns):
        return None
    values = dict(zip(columns, key))
    business_columns = [c for c in columns if c not in namespaces]
    if not business_columns:
        return None
    return namespaces, [values[c] for c in namespaces], business_columns, [values[c] for c in business_columns]


def detect_role_occurrences(rules: list[RuleSpec], bindings: dict, *, max_bindings=5000,
                            max_roles_per_key=16, max_events=128, max_working_bytes=1024*1024):
    """Return ``(events, coverage)`` from successfully validated rule bindings.

    The caller supplies only rules whose current execution succeeded. Each
    event is a table-local work reference; cross-table matches produce one
    event for each affected table. All indexes containing source keys expire
    with this call. Bounds are explicit so a common code cannot cause an
    unbounded all-pairs search. Existing reuse of one entity is not a new role.
    """
    limits = (max_bindings, max_roles_per_key, max_events, max_working_bytes)
    if any(type(value) is not int or value < 1 for value in limits):
        raise ValueError('invalid_role_detection_limit')
    by_id = {r.rule_id: r for r in rules}
    index = defaultdict(list)
    events = {}
    coverage = {'bindings_checked': 0, 'binding_limit_omissions': 0,
                'role_limit_omissions': 0, 'event_limit_omissions': 0,
                'memory_limit_reached': 0}
    working_bytes = 0
    for (rule_id, raw_key), entity in bindings.items():
        rule = by_id.get(rule_id)
        if rule is None:
            continue
        if coverage['bindings_checked'] >= max_bindings:
            coverage['binding_limit_omissions'] += 1
            continue
        working_bytes += len(raw_key.encode('utf-8')) + len(entity.encode('utf-8')) + 128
        if working_bytes > max_working_bytes:
            coverage['memory_limit_reached'] = 1
            break
        coverage['bindings_checked'] += 1
        key = json.loads(raw_key)
        if not isinstance(key, list):
            continue
        role = _role(rule, key)
        if role is None:
            continue
        namespaces, scope_values, columns, values = role
        if any(v is None or v == '' for v in scope_values + values):
            continue
        # Namespace column names remain part of the comparison. Coincidentally
        # equal keys in different tenant domains cannot create a shared scope.
        token = canonical([rule.identity.entity_kind, namespaces, scope_values, values])
        source = (rule.source.source_id, rule.source.schema_name, rule.source.table)
        physical = source + (tuple(columns),)
        bucket = index[token]
        if any(previous['physical'] == physical for previous in bucket):
            continue
        if len(bucket) >= max_roles_per_key:
            coverage['role_limit_omissions'] += 1
            continue
        current = {'physical': physical, 'rule': rule, 'entity': entity}
        for previous in bucket:
            if previous['entity'] == entity:
                continue
            pair = [previous, current]
            rule_ids = tuple(sorted({item['rule'].rule_id for item in pair}))
            column_refs = tuple(sorted({'.'.join(item['physical'][:3]) + '.' + column
                for item in pair for column in item['physical'][3]}))
            for affected in {item['physical'][:3] for item in pair}:
                signature = (affected, rule_ids, column_refs)
                if signature not in events:
                    if len(events) >= max_events:
                        coverage['event_limit_omissions'] += 1
                        continue
                    events[signature] = 0
                events[signature] += 1
        bucket.append(current)
    result = [RoleOccurrence(SourceRef(source_id=source[0], schema=source[1], table=source[2]),
                rule_ids, columns, count)
        for (source, rule_ids, columns), count in sorted(events.items())]
    return result, coverage
