"""Compact, versioned evidence for an agent consuming an authorized retrieval result."""
from __future__ import annotations

from collections import deque
import json

from .path_evidence import BINDING_FIELDS


def _compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _fields(item, names):
    return {key: item[key] for key in names if item.get(key) not in (None, '', [], {})}


def agent_context(answer, *, version=None, sources=()):
    """Serialize only evidence already visible to this query. Never fetch an expanded graph."""
    plan = answer.get('plan') or {}
    partial = answer.get('available_context') or {}
    kinds = {s.id: s.kind for s in sources}
    context = {
        'format': 'lightgraph.agent-context.v1', 'graph_version': version,
        'question': answer.get('query', ''), 'access_scopes': answer.get('scopes', []),
        'selection': plan.get('business_context'),
        'intent': plan.get('intent'), 'unresolved': plan.get('unresolved', []),
        'grouping': _fields(answer.get('grouping') or {}, ('subject', 'axis', 'filters', 'grouping_applicability')),
        'unverified_conditions': plan.get('unverified_predicates', []),
        'unbound_mentions': plan.get('unbound_mentions', []),
        'overview': _fields(answer.get('overview') or {}, ('status', 'counts', 'ranking_metric', 'sampled')),
        'candidate_limit': bool(plan.get('candidate_limit')),
        'dimensional': _fields(answer.get('dimensional') or {}, ('status', 'stop_reason')),
        'traverse': _fields(answer.get('traverse') or {}, ('status',)),
        'conditions': [{'term': s.get('text'), 'match': s.get('match'),
                        'candidate_ids': [n['node_id'] for n in s.get('candidates', [])]}
                       for s in plan.get('conditions', [])],
        'excluded_terms': plan.get('excluded_terms', []), 'partial_status': partial.get('status'),
        'truncated': bool(answer.get('truncated') or partial.get('truncated')),
        'nodes': [], 'edges': [], 'bindings': [], 'paths': [], 'joint_witnesses': [], 'source_passages': [],
    }
    # Separate budgets keep a large entity list from crowding out paths and physical mappings.
    remaining = dict(nodes=6000, edges=6000, bindings=10000, paths=4000, joint_witnesses=3000, source_passages=1200)

    def add(section, row):
        size = len(_compact(row).encode('utf-8')) + 1
        if size > remaining[section]:
            context['truncated'] = True
            return False
        remaining[section] -= size
        context[section].append(row)
        return True

    def observations(item):
        seen = set()
        for obs in item.get('observations', []):
            prov = obs.get('provenance') or {}
            source = prov.get('source') or {}
            mapping = _fields(prov, BINDING_FIELDS)
            row = _fields(obs, ('business_context', 'scopes', 'required_scope_refs', 'source_tables', 'evidence_refs', 'properties'))
            if obs.get('identity_decision_refs') or obs.get('identity_reviewed'):
                row['identity_reviewed'] = True
                if obs.get('source_identity') and mapping.get('key_columns'):
                    row['source_identity'] = obs['source_identity']
            if source.get('table'):
                row.update(source=_fields(source, ('source_id', 'schema', 'table')),
                           source_kind=kinds.get(source.get('source_id')), mapping=mapping)
            if row:
                key = _compact(row)
                if key not in seen:
                    seen.add(key)
                    yield {'ref': item['id'], **row}

    # Prefer whole named paths when a large result needs clipping.
    paths = []
    routes = {key: (answer.get(key) or {}).get('paths', []) for key in ('grouping', 'traverse', 'drilldown')}
    routes['association'] = answer.get('association_paths', [])
    for section, candidates in routes.items():
        for path in candidates:
            paths.append({'operation': section, **_fields(path, ('node_ids', 'edge_ids', 'business_context',
                'context_status', 'observed_path', 'stop_reason', 'matched_terms', 'match'))})
    priority = {nid for path in paths for nid in path.get('node_ids', [])}
    priority_edges = {eid for path in paths for eid in path.get('edge_ids', [])}
    node_ids, edge_ids = set(), set()
    supported = []
    for label, graph in [('result', answer), ('partial', partial)]:
        for node in sorted(graph.get('nodes', []), key=lambda n: n['id'] not in priority):
            if node['id'] in node_ids:
                continue
            row = _fields(node, ('id', 'kind', 'display_name', 'identity', 'identity_types', 'description'))
            retained = add('nodes', {'part': label, **row})
            if not retained and 'description' in row:
                retained = add('nodes', {'part': label, **_fields(row, ('id', 'kind', 'display_name', 'identity',
                                                                        'identity_types')), 'description_omitted': True})
            if retained:
                node_ids.add(node['id'])
                supported.append(node)
        for edge in sorted(graph.get('edges', []), key=lambda e: e['id'] not in priority_edges):
            if edge['id'] in edge_ids:
                continue
            if edge.get('source') not in node_ids or edge.get('target') not in node_ids:
                context['truncated'] = True
                continue
            row = _fields(edge, ('id', 'source', 'relation', 'target', 'qualifiers', 'source_tables', 'evidence_refs'))
            if add('edges', {'part': label, **row}):
                edge_ids.add(edge['id'])
                supported.append(edge)
    # Give each selected path endpoint and relationship a turn before including
    # another observation from a heavily referenced entity.
    priority_refs = priority | priority_edges
    pending = deque(observations(item) for item in sorted(supported, key=lambda item: item['id'] not in priority_refs))
    while pending and remaining['bindings'] > 0:
        stream = pending.popleft()
        binding = next(stream, None)
        if binding is not None:
            retained = add('bindings', binding)
            if not retained and 'properties' in binding:
                # Preserve an entire physical mapping even when one property is document-sized.
                binding.pop('properties')
                retained = add('bindings', {**binding, 'properties_omitted': True})
            if retained:
                pending.append(stream)
    if pending:
        context['truncated'] = True
    for path in paths:
        if set(path.get('node_ids', [])) <= node_ids and set(path.get('edge_ids', [])) <= edge_ids:
            add('paths', path)
        else:
            context['truncated'] = True
    for label, section in [('result', answer.get('dimensional') or {}), ('partial', partial)]:
        for witness in section.get('witnesses', []):
            if (witness.get('fact_id') in node_ids and set(witness.get('edge_ids', [])) <= edge_ids
                    and all(c.get('node_id') in node_ids for c in witness.get('conditions', []))):
                add('joint_witnesses', {'part': label, **_fields(witness, ('fact_id', 'row_id', 'conditions',
                                                                           'edge_ids', 'business_context', 'common_record'))})
            else:
                context['truncated'] = True
    for passage in partial.get('passages', []):
        add('source_passages', _fields(passage, ('excerpt', 'node_id', 'field', 'source_tables', 'evidence_refs')))
    return context
