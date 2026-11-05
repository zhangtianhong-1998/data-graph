"""Execute generic source mappings without inferring business types or relations."""
from __future__ import annotations

import time
from collections import defaultdict, deque
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone

from lightgraph.contracts import canonical, digest
from lightgraph.errors import BudgetExceeded, LightGraphError
from lightgraph.query import QueryCompiler
from lightgraph.sources.base import aggregate_count
from .models import GraphDraft, iter_source_mappings
from .access import scope_binding_map, support_scope
from .display import derive_display_graph


def _projection_columns(mapping):
    if hasattr(mapping, 'match_columns'):
        return list(dict.fromkeys(mapping.match_columns + mapping.key_columns))
    keys = (mapping.key_columns if hasattr(mapping, 'key_columns') else
            mapping.source_key_columns + mapping.target_key_columns)
    columns = list(keys) + list(mapping.properties.values()) + mapping.context_columns
    if hasattr(mapping, 'alias_columns'):
        columns += mapping.alias_columns
        columns += list(mapping.display_columns.values())
        if mapping.label_column:
            columns.append(mapping.label_column)
    return list(dict.fromkeys(columns))


projection_columns = _projection_columns


def _has_cycle(pairs):
    outgoing = defaultdict(set)
    incoming = defaultdict(int)
    for source, target in pairs:
        if target not in outgoing[source]:
            outgoing[source].add(target)
            incoming[target] += 1
        incoming.setdefault(source, 0)
    ready = deque(node for node, count in incoming.items() if not count)
    visited = 0
    while ready:
        node = ready.popleft()
        visited += 1
        for target in outgoing[node]:
            incoming[target] -= 1
            if not incoming[target]:
                ready.append(target)
    return visited != len(incoming)


def execute_draft(draft, adapters, tables, *, goal=None, max_working_bytes=32 * 1024 * 1024,
                  max_rows=20000, max_seconds=1800, scope_bindings=()):
    """Check complete source projections and build an evidence-labelled graph.

    Callers provide an authorized physical catalog. Keys include only explicitly
    mapped values; source names and physical positions never enter identity.
    All mapping results remain unpublishable when any data check fails.
    """
    draft = GraphDraft.model_validate(draft.model_dump(mode='python', by_alias=True)
                                     if isinstance(draft, GraphDraft) else draft)
    allowed = {(t.source_id, t.schema_name, t.name): t for t in tables}
    by_source = {t.source_id: adapter for adapter in adapters for t in adapter.describe()}
    nodes, edges, diagnostics = {}, {}, []
    checked, projected_rows, omitted = 0, 0, 0
    deadline = time.monotonic() + max_seconds
    graph_bytes = 0
    cache_bytes = 0
    lookup_bytes = 0
    projection_cache, lookup_indexes = {}, {}
    cache_hits = 0
    lookup_matches, lookup_omitted = 0, 0
    source_read_windows = []
    visibility = scope_binding_map(scope_bindings)

    def scope_columns(mapping):
        return visibility.get((mapping.source_id, mapping.schema_name, mapping.table), [])

    def clock():
        if time.monotonic() > deadline:
            raise LightGraphError('construction_time_exhausted')

    @contextmanager
    def source_snapshot(source_id, adapter):
        # These are client-observed transaction windows, not server snapshot IDs
        # or a distributed point-in-time guarantee across different sources.
        window = {'source_id': source_id, 'requested_at': datetime.now(timezone.utc).isoformat(),
                  'basis': 'client_observed_snapshot_context', 'state': 'opening'}
        source_read_windows.append(window)
        try:
            with adapter.snapshot():
                window.update(opened_at=datetime.now(timezone.utc).isoformat(), state='open')
                yield adapter
            window['state'] = 'closed'
        except Exception:
            window['state'] = 'failed'
            raise
        finally:
            window['closed_at'] = datetime.now(timezone.utc).isoformat()

    def diagnostic(code, **location):
        entry = {'code': code, **location}
        if entry not in diagnostics:
            diagnostics.append(entry)

    def update_size(previous, current):
        nonlocal graph_bytes
        graph_bytes += len(canonical(current).encode()) - previous
        if graph_bytes + cache_bytes + lookup_bytes > max_working_bytes:
            raise LightGraphError('memory_budget_exhausted')

    def rows_for(mapping):
        nonlocal cache_bytes, cache_hits
        clock()
        table = allowed.get((mapping.source_id, mapping.schema_name, mapping.table))
        adapter = by_source.get(mapping.source_id)
        if table is None or adapter is None:
            raise LightGraphError('unauthorized_object')
        columns = list(dict.fromkeys(_projection_columns(mapping) + scope_columns(mapping)))
        if not columns or set(columns) - set(table.column_names):
            raise LightGraphError('unauthorized_column')
        if goal is not None:
            from lightgraph.discovery.goal import column_is_evidence_only, column_is_excluded, source_in_goal
            if not source_in_goal(goal, table):
                raise LightGraphError('outside_source_goal')
            if any(column_is_excluded(goal, table, c) for c in columns):
                raise LightGraphError('excluded_column_projected')
            if any(column_is_evidence_only(goal, table, c) for c in columns):
                raise LightGraphError('evidence_only_column_projected')
            table = table.model_copy(update={'columns': [c for c in table.columns
                if not column_is_excluded(goal, table, c.name)]})
        cap = min(max_rows, adapter.limits.max_result_rows)
        compiler = QueryCompiler([table], dialect=adapter.compiler.dialect,
                                 physical_columns=adapter.compiler.guard.physical_columns)
        # Compile against the filtered catalog before the adapter uses its own
        # catalog, including columns mentioned only in a predicate.
        compiler.projection(table, columns, predicate=mapping.predicate, limit=cap)
        cache_key = canonical([mapping.source_id, mapping.schema_name, mapping.table,
                               sorted(columns), mapping.predicate, cap])
        if cache_key in projection_cache:
            cache_hits += 1
            return projection_cache[cache_key]
        query = compiler.profile_column_group(table, columns, predicate=mapping.predicate)
        expected = None
        for batch in adapter.execute(query, row_limit=1):
            with batch:
                for row in batch:
                    if expected is not None:
                        raise LightGraphError('invalid_aggregate_shape')
                    expected = aggregate_count(dict(zip(batch.columns, row)).get('distinct_tuples'))
        if expected is None:
            raise LightGraphError('invalid_aggregate_shape')
        if expected > cap:
            raise LightGraphError('projection_truncated')
        rows, retained_bytes = [], 0
        stream = adapter.project(table, columns, predicate=mapping.predicate, distinct=True, limit=cap)
        try:
            for batch in stream:
                with batch:
                    if tuple(batch.columns) != tuple(columns):
                        raise LightGraphError('projection_columns_mismatch')
                    for row in batch:
                        clock()
                        item = dict(zip(batch.columns, row))
                        retained_bytes += len(canonical(item).encode())
                        if retained_bytes + graph_bytes + cache_bytes + lookup_bytes > max_working_bytes:
                            raise LightGraphError('memory_budget_exhausted')
                        if len(rows) >= cap:
                            raise LightGraphError('projection_truncated')
                        rows.append(item)
        finally:
            if hasattr(stream, 'close'):
                stream.close()
        if len(rows) != expected:
            raise LightGraphError('projection_incomplete')
        # The cache belongs to this execute_draft's open source snapshots only.
        # Retain complete authorized projections, never failed partial streams.
        cache_bytes += retained_bytes
        projection_cache[cache_key] = rows
        return rows

    def identity(kind, key):
        return 'n_' + digest(kind, key)

    def key_for(row, columns):
        key = [row[c] for c in columns]
        if any(v is None or v == '' for v in key):
            raise LightGraphError('null_identity_key')
        return key

    def resolve_endpoint(mapping, row, end):
        """Resolve only matches for this input row; unrelated sets never combine."""
        nonlocal lookup_bytes, lookup_matches, lookup_omitted
        columns = getattr(mapping, end + '_key_columns')
        lookup = getattr(mapping, end + '_lookup')
        if lookup is None:
            return [(key_for(row, columns), None)]
        cache_key = canonical(lookup.model_dump(mode='json', by_alias=True))
        if cache_key not in lookup_indexes:
            index = defaultdict(list)
            for candidate in rows_for(lookup):
                clock()
                match = [candidate[c] for c in lookup.match_columns]
                if any(v is None or v == '' for v in match):
                    continue
                token = canonical(match)
                lookup_bytes += len(token.encode()) + 16
                if graph_bytes + cache_bytes + lookup_bytes > max_working_bytes:
                    raise LightGraphError('memory_budget_exhausted')
                index[token].append(candidate)
            lookup_indexes[cache_key] = index
        values = [row[c] for c in columns]
        matched = lookup_indexes[cache_key].get(canonical(values), [])
        if not matched:
            if lookup.on_missing == 'omit':
                lookup_omitted += 1
                return []
            raise LightGraphError('lookup_reference_unmatched')
        resolved = [(key_for(candidate, lookup.key_columns), candidate) for candidate in matched]
        if lookup.cardinality == 'one' and len({canonical(key) for key, _ in resolved}) > 1:
            raise LightGraphError('lookup_reference_ambiguous')
        answers = []
        for key, candidate in resolved:
            origin = {'source_id': lookup.source_id, 'schema': lookup.schema_name, 'table': lookup.table}
            details = {'endpoint': end, 'source': origin, 'match_columns': lookup.match_columns,
                       'key_columns': lookup.key_columns, 'input_columns': columns,
                       'input_values': values, 'resolved_key': key,
                       'scope_ref': support_scope(candidate, scope_columns(lookup))}
            content = {c: candidate[c] for c in sorted(set(_projection_columns(lookup) + scope_columns(lookup)))}
            details['row_context'] = {'id': 'ctx_' + digest(origin, content), 'values': content}
            answers.append((key, details))
            lookup_matches += 1
        return answers

    def provenance(kind, index, mapping):
        return {'definition': kind, 'mapping': index,
                'source': {'source_id': mapping.source_id, 'schema': mapping.schema_name,
                           'table': mapping.table},
                'columns': _projection_columns(mapping), 'scope_columns': scope_columns(mapping),
                **({'constant_properties': mapping.constant_properties} if mapping.constant_properties else {}),
                **({'context_columns': mapping.context_columns} if mapping.context_columns else {})}

    def contribution(mapping, row, origin):
        support = {'scope_ref': support_scope(row, scope_columns(mapping)),
                   'provenance': origin, 'properties': {}}
        properties(support, mapping, row)
        if mapping.context_columns:
            # A stable content key links projections of the same authorized row
            # context. Equal rows share evidence; this is not a physical row ID.
            values = {name: row[name] for name in sorted(mapping.context_columns)}
            support['row_context'] = {'id': 'ctx_' + digest(origin['source'], values), 'values': values}
        if hasattr(mapping, 'alias_columns'):
            value = row[mapping.label_column] if mapping.label_column else None
            support['label'] = str(value) if value is not None else None
            support['aliases'] = sorted({str(row[column]) for column in mapping.alias_columns
                                         if row[column] is not None})
            if mapping.display_columns:
                support['display_values'] = {name: row[column] for name, column in mapping.display_columns.items()
                                             if row[column] is not None}
        return support

    def properties(item, mapping, row):
        for name, value in mapping.constant_properties.items():
            values = item['properties'].setdefault(name, [])
            if canonical(value) not in {canonical(v) for v in values}:
                values.append(value)
        for name, column in mapping.properties.items():
            value = row[column]
            if value is None:
                continue
            values = item['properties'].setdefault(name, [])
            if canonical(value) not in {canonical(v) for v in values}:
                values.append(value)

    definitions = {'entities': [e.model_dump(mode='json', exclude={'mappings'}) for e in draft.entities],
                   'relations': [r.model_dump(mode='json', exclude={'mappings'}) for r in draft.relations]}
    if not draft.entities:
        diagnostic('graph_draft_has_no_entities')
    try:
        used_sources = {mapping.source_id for mapping in iter_source_mappings(draft)}
        with ExitStack() as stack:
            for source_id in sorted(used_sources):
                adapter = by_source.get(source_id)
                if adapter is None:
                    continue
                capability = adapter.capabilities()
                if any(capability.capabilities.get(c) != 'verified' for c in
                       ('metadata_reading', 'read_only_transaction', 'parameter_binding', 'distinct_projection')):
                    raise LightGraphError('snapshot_or_projection_unavailable')
                stack.enter_context(source_snapshot(source_id, adapter))
                # Database adapters attest snapshot consistency only while the
                # read-only transaction is open; files can attest it at rest.
                if not adapter.capabilities().consistent_snapshot:
                    raise LightGraphError('snapshot_or_projection_unavailable')
            for entity in draft.entities:
                if goal is not None and goal.entity_kinds and entity.type not in goal.entity_kinds:
                    diagnostic('outside_extraction_goal', entity_type=entity.type)
                    continue
                for index, mapping in enumerate(entity.mappings):
                    location = {'entity_type': entity.type, 'mapping': index}
                    try:
                        rows = rows_for(mapping)
                        checked += 1
                        projected_rows += len(rows)
                        support = provenance(entity.type, index, mapping)
                        support['key_columns'] = mapping.key_columns
                        if mapping.label_column:
                            support['label_column'] = mapping.label_column
                        if mapping.display_columns:
                            support['display_columns'] = mapping.display_columns
                        for row in rows:
                            clock()
                            key = key_for(row, mapping.key_columns)
                            node_id = identity(entity.type, key)
                            label = row[mapping.label_column] if mapping.label_column else None
                            current_support = contribution(mapping, row, support)
                            node = nodes.get(node_id)
                            previous = len(canonical(node).encode()) if node is not None else 0
                            if node is None:
                                node = nodes[node_id] = {'id': node_id, 'type': entity.type,
                                    'label': str(label) if label is not None else '',
                                    'label_observed': label is not None,
                                    'identity': dict(zip(entity.key_names, key)), 'aliases': [],
                                    'properties': {}, 'provenance': [], 'supports': []}
                            elif label is not None:
                                if any(previous_support['scope_ref'] == current_support['scope_ref']
                                       and previous_support.get('label') is not None
                                       and previous_support['label'] != str(label)
                                       for previous_support in node['supports']):
                                    diagnostic('same_identity_conflicting_label', node_id=node_id, **location)
                                elif not node['label_observed']:
                                    node['label'], node['label_observed'] = str(label), True
                            for column in mapping.alias_columns:
                                alias = row[column]
                                if alias is not None and str(alias) not in node['aliases']:
                                    node['aliases'].append(str(alias))
                            properties(node, mapping, row)
                            if support not in node['provenance']:
                                node['provenance'].append(support)
                            if current_support not in node['supports']:
                                node['supports'].append(current_support)
                            update_size(previous, node)
                    except LightGraphError as exc:
                        if isinstance(exc, BudgetExceeded) or exc.code in {'memory_budget_exhausted', 'construction_time_exhausted',
                                       'run_time_budget_exhausted', 'query_budget_exhausted'}:
                            raise
                        diagnostic(exc.code, **location)
            for relation in draft.relations:
                if goal is not None and goal.relation_kinds and relation.type not in goal.relation_kinds:
                    diagnostic('outside_relation_goal', relation_type=relation.type)
                    continue
                pairs = set()
                for index, mapping in enumerate(relation.mappings):
                    location = {'relation_type': relation.type, 'mapping': index}
                    try:
                        rows = rows_for(mapping)
                        checked += 1
                        projected_rows += len(rows)
                        support = provenance(relation.type, index, mapping)
                        support.update(source_key_columns=mapping.source_key_columns,
                                       target_key_columns=mapping.target_key_columns)
                        for end in ('source', 'target'):
                            lookup = getattr(mapping, end + '_lookup')
                            if lookup is not None:
                                support[end + '_lookup'] = lookup.model_dump(mode='json', by_alias=True)
                        for row in rows:
                            clock()
                            sources = resolve_endpoint(mapping, row, 'source')
                            if not sources:
                                # Explicitly omitted input rows cannot produce
                                # a pair and must not fail on an unused target.
                                continue
                            targets = resolve_endpoint(mapping, row, 'target')
                            # Multiple matches are allowed only when the lookup
                            # explicitly declares many, and only for this row.
                            for source_key, source_match in sources:
                                for target_key, target_match in targets:
                                    clock()
                                    source = identity(relation.source_type, source_key)
                                    target = identity(relation.target_type, target_key)
                                    if source not in nodes or target not in nodes:
                                        diagnostic('unresolved_relation_endpoint', **location)
                                        continue
                                    if source == target:
                                        if mapping.equal_endpoints == 'omit':
                                            omitted += 1
                                            continue
                                        if mapping.equal_endpoints == 'error' or not relation.allow_self_links:
                                            diagnostic('self_link_forbidden', **location)
                                            continue
                                    pairs.add((source, target))
                                    edge_id = 'r_' + digest(relation.type, source, target)
                                    edge = edges.get(edge_id)
                                    previous = len(canonical(edge).encode()) if edge is not None else 0
                                    if edge is None:
                                        edge = edges[edge_id] = {'id': edge_id, 'type': relation.type,
                                            'source': source, 'target': target, 'properties': {}, 'provenance': [], 'supports': []}
                                    properties(edge, mapping, row)
                                    if support not in edge['provenance']:
                                        edge['provenance'].append(support)
                                    current_support = contribution(mapping, row, support)
                                    matches = [match for match in (source_match, target_match) if match is not None]
                                    if matches:
                                        current_support['lookup_matches'] = matches
                                        current_support['required_scope_refs'] = sorted({match['scope_ref'] for match in matches})
                                        if 'row_context' not in current_support:
                                            values = {c: row[c] for c in sorted(set(_projection_columns(mapping) + scope_columns(mapping)))}
                                            current_support['row_context'] = {
                                                'id': 'ctx_' + digest(support['source'], values), 'values': values}
                                    if current_support not in edge['supports']:
                                        edge['supports'].append(current_support)
                                    update_size(previous, edge)
                    except LightGraphError as exc:
                        if isinstance(exc, BudgetExceeded) or exc.code in {'memory_budget_exhausted', 'construction_time_exhausted',
                                       'run_time_budget_exhausted', 'query_budget_exhausted'}:
                            raise
                        diagnostic(exc.code, **location)
                if relation.acyclic and _has_cycle(pairs):
                    diagnostic('relation_cycle', relation_type=relation.type)
    except LightGraphError as exc:
        diagnostic(exc.code)
        if exc.code == 'memory_budget_exhausted':
            # Do not return an over-limit graph after stopping its enumeration.
            # The source mappings remain durable and can be retried unchanged.
            nodes.clear()
            edges.clear()
    except Exception:
        # Driver exceptions may contain SQL, credentials or source values.
        # Preserve the draft as failed validation without returning their body
        # through MCP or persisting it in the validation evidence.
        diagnostic('construction_source_execution_failed')
    passed = not diagnostics
    graph = {'format_version': 1, 'target': 'graph', 'status': 'validated' if passed else 'candidate',
             'support_visibility_version': 1,
             'semantic_origin': 'agent_authored', 'purpose': draft.purpose or (goal.purpose if goal else ''),
             'definitions': definitions, 'nodes': sorted(nodes.values(), key=lambda n: n['id']),
             'edges': sorted(edges.values(), key=lambda e: e['id']), 'evidence_refs': draft.evidence_refs}
    for item in graph['nodes']:
        item.pop('label_observed', None)
        observed = sorted({support['label'] for support in item['supports'] if support.get('label') is not None})
        if observed:
            item['label'] = observed[0]
        item['aliases'].sort()
    for item in graph['nodes'] + graph['edges']:
        for values in item['properties'].values():
            values.sort(key=canonical)
        item['provenance'].sort(key=canonical)
        item['supports'].sort(key=canonical)
    return {'passed': passed, 'diagnostics': diagnostics,
            'source_read_windows': source_read_windows,
            'metrics': {'mappings_checked': checked, 'projected_distinct_rows': projected_rows,
                        'nodes': len(nodes), 'edges': len(edges), 'equal_endpoints_omitted': omitted,
                        'projection_cache_hits': cache_hits, 'unique_projections': len(projection_cache),
                        'lookup_matches': lookup_matches, 'lookup_inputs_omitted': lookup_omitted},
            'graph': derive_display_graph(graph)}
