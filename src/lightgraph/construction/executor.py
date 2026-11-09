"""Execute generic source mappings without inferring business types or relations."""
from __future__ import annotations

import time
from collections import Counter, defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timezone

from lightgraph.contracts import canonical, digest
from lightgraph.errors import BudgetExceeded, LightGraphError
from lightgraph.query import QueryCompiler
from lightgraph.sources.base import aggregate_count
from lightgraph.source_values import (source_scalar, scalar_kind, typed_key, identity_values,
    typed_value, plain_value, SOURCE_KEY_SEMANTICS_VERSION)
from .models import GraphDraft, draft_evidence_refs
from .access import scope_binding_map, support_scope
from .display import derive_display_graph
from .accumulation import GraphAccumulator
from .metadata_columns import (is_metadata_mapping, physical_column_key, metadata_endpoint,
                               mapping_predicate, checked_column, column_node, merge_column_supports)


def _projection_columns(mapping):
    if is_metadata_mapping(mapping):
        return [mapping.column]
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
                  max_rows=20000, max_seconds=1800, scope_bindings=(), cancelled=None):
    """Check complete source projections and build an evidence-labelled graph.

    Callers provide an authorized physical catalog. Keys include only explicitly
    mapped values; source names and physical positions never enter identity.
    Full validation fails on counterexamples; node-local label conflicts may
    subsequently be quarantined for publication of the verified remainder.
    ``cancelled`` lets a caller stop CPU-side materialization cooperatively;
    source adapters remain responsible for interrupting a blocking query.
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
    accumulation_index_bytes = 0
    peak_working_bytes = 0
    projection_cache, lookup_indexes = {}, {}
    cache_hits = 0
    lookup_matches, lookup_omitted = 0, 0
    source_read_windows = []
    unavailable_sources = {}
    incomplete_entity_types = set()
    visibility = scope_binding_map(scope_bindings)

    def scope_columns(mapping):
        return visibility.get((mapping.source_id, mapping.schema_name, mapping.table), [])

    def clock():
        if cancelled is not None and cancelled():
            raise LightGraphError('construction_cancelled')
        if time.monotonic() > deadline:
            raise LightGraphError('construction_time_exhausted')

    def projection_key(mapping):
        adapter = by_source.get(mapping.source_id)
        cap = min(max_rows, adapter.limits.max_result_rows) if adapter else max_rows
        return canonical([mapping.source_id, mapping.schema_name, mapping.table,
            sorted(set(_projection_columns(mapping) + scope_columns(mapping))), mapping.predicate,
            getattr(mapping, 'metadata_basis', None),
            physical_column_key(metadata_endpoint(mapping)[1]) if metadata_endpoint(mapping)[1] else None, cap])

    # Keep a projection only until its last consumer. Lookup indexes retain
    # their backing rows for this snapshot and therefore remain pinned.
    remaining_uses = Counter(projection_key(m) for d in draft.entities + draft.relations for m in d.mappings
                             if not is_metadata_mapping(m))
    pinned_projections = {projection_key(lookup) for d in draft.relations for m in d.mappings
                          for lookup in (m.source_lookup, m.target_lookup) if lookup is not None}
    projection_sizes = {}
    unique_projections = set()

    def release_projection(mapping):
        nonlocal cache_bytes
        if is_metadata_mapping(mapping):
            return
        key = projection_key(mapping)
        remaining_uses[key] -= 1
        if remaining_uses[key] <= 0 and key not in pinned_projections:
            projection_cache.pop(key, None)
            cache_bytes -= projection_sizes.pop(key, 0)

    def blocked_sources(mapping):
        ids = {mapping.source_id}
        for name in ('source_lookup', 'target_lookup'):
            lookup = getattr(mapping, name, None)
            if lookup:
                ids.add(lookup.source_id)
        return sorted(ids.intersection(unavailable_sources))

    @contextmanager
    def source_snapshot(source_id, adapter, projection):
        # These are client-observed transaction windows, not server snapshot IDs
        # or a distributed point-in-time guarantee across different sources.
        window = {'source_id': source_id, 'requested_at': datetime.now(timezone.utc).isoformat(),
                  'basis': 'client_observed_snapshot_context', 'state': 'opening',
                  'projection_fingerprint': digest(projection), 'completed_projections': 0}
        source_read_windows.append(window)
        body_error = None
        try:
            capability = adapter.capabilities()
            if any(capability.capabilities.get(c) != 'verified' for c in
                   ('metadata_reading', 'read_only_transaction', 'parameter_binding', 'distinct_projection')):
                raise LightGraphError('snapshot_or_projection_unavailable')
            with adapter.snapshot():
                if not adapter.capabilities().consistent_snapshot:
                    raise LightGraphError('snapshot_or_projection_unavailable')
                window.update(opened_at=datetime.now(timezone.utc).isoformat(), state='open')
                try:
                    yield window
                except BaseException as exc:
                    body_error = exc
                    raise
            window['state'] = 'closed'
        except Exception as exc:
            # An exception from another mapping is not evidence that this
            # source failed to open its own transaction.
            interrupted = exc is body_error
            window['state'] = 'closed_after_interruption' if interrupted else 'failed'
            if not interrupted and isinstance(exc, LightGraphError):
                exc.construction_source_id = source_id
                exc.construction_mapping_location = {'phase': 'snapshot_open' if 'opened_at' not in window else 'snapshot_close'}
            if not isinstance(exc, LightGraphError):
                # Preserve source isolation for unexpected adapter failures,
                # without exposing a driver's SQL/credentials in its message.
                failure = LightGraphError('construction_source_execution_failed')
                failure.construction_source_id = source_id
                failure.construction_mapping_location = {'phase': 'projection' if interrupted else
                    'snapshot_open' if 'opened_at' not in window else 'snapshot_close'}
                raise failure from None
            raise
        finally:
            window['closed_at'] = datetime.now(timezone.utc).isoformat()

    def diagnostic(code, **location):
        entry = {'code': code, **location}
        if entry not in diagnostics:
            diagnostics.append(entry)

    def error_diagnostic(error, **location):
        from lightgraph.sources.database_errors import safe_database_diagnostic
        detail = safe_database_diagnostic(error)
        location = {**getattr(error, 'construction_mapping_location', {}), **location}
        source_id = getattr(error, 'construction_source_id', None)
        if source_id in by_source:
            location['source_id'] = source_id
        diagnostic(error.code, **location, retryable=error.retryable,
                   **({'database': detail} if detail else {}),
                   **getattr(error, 'construction_memory_diagnostic', {}))

    def failed_operation(error):
        from .failures import SOURCE_FAILURES
        source_id = getattr(error, 'construction_source_id', None)
        if source_id in by_source and error.code in SOURCE_FAILURES:
            # A failed statement can invalidate the open read transaction.
            # Never retry later mappings against that same snapshot.
            unavailable_sources[source_id] = error.code

    def check_memory(phase, pending_projection_bytes=0):
        nonlocal peak_working_bytes
        required = (graph_bytes + cache_bytes + lookup_bytes + accumulation_index_bytes
                    + pending_projection_bytes)
        peak_working_bytes = max(peak_working_bytes, required)
        if required > max_working_bytes:
            error = LightGraphError('memory_budget_exhausted')
            error.construction_memory_diagnostic = {
                'phase': phase, 'limit_bytes': max_working_bytes, 'estimated_required_bytes': required,
                'graph_payload_bytes': graph_bytes, 'projection_cache_bytes': cache_bytes,
                'pending_projection_bytes': pending_projection_bytes, 'lookup_index_bytes': lookup_bytes,
                'accumulation_index_bytes': accumulation_index_bytes,
                'measurement': 'serialized_payload_and_estimated_indexes', 'exact_rss': False}
            raise error

    def account_graph(delta, index_delta=0):
        nonlocal graph_bytes, accumulation_index_bytes
        graph_bytes += delta
        accumulation_index_bytes += index_delta
        check_memory('graph_accumulation')

    accumulation = GraphAccumulator(account_graph)

    def update_size(previous, current):
        # Catalog objects are small and updated only once per mapping.
        account_graph(len(canonical(current).encode()) - previous)

    def rows_for(mapping):
        try:
            return _rows_for(mapping)
        except LightGraphError as exc:
            exc.construction_source_id = mapping.source_id
            raise

    def _rows_for(mapping):
        nonlocal cache_bytes, cache_hits
        clock()
        table = allowed.get((mapping.source_id, mapping.schema_name, mapping.table))
        adapter = by_source.get(mapping.source_id)
        if table is None or adapter is None:
            raise LightGraphError('unauthorized_object')
        _, column_endpoint = metadata_endpoint(mapping)
        if column_endpoint is not None:
            checked_column(column_endpoint, allowed, goal)
        predicate = mapping_predicate(mapping)
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
        compiler.projection(table, columns, predicate=predicate, limit=cap)
        cache_key = projection_key(mapping)
        if cache_key in projection_cache:
            cache_hits += 1
            return projection_cache[cache_key]
        # A count and its projection need one snapshot. Release it before
        # materializing or reading another projection, including another source.
        with source_snapshot(mapping.source_id, adapter, cache_key) as window:
            query = compiler.profile_column_group(table, columns, predicate=predicate)
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
            stream = adapter.project(table, columns, predicate=predicate, distinct=True, limit=cap)
            try:
                for batch in stream:
                    with batch:
                        if tuple(batch.columns) != tuple(columns):
                            raise LightGraphError('projection_columns_mismatch')
                        for row in batch:
                            clock()
                            item = dict(zip(batch.columns, (source_scalar(value) for value in row)))
                            retained_bytes += len(canonical(item).encode())
                            check_memory('projection', retained_bytes)
                            if len(rows) >= cap:
                                raise LightGraphError('projection_truncated')
                            rows.append(item)
            finally:
                if hasattr(stream, 'close'):
                    stream.close()
            if len(rows) != expected:
                raise LightGraphError('projection_incomplete')
            window['completed_projections'] = 1
        # Reuse only complete observations from this validation; the window above
        # records when each projection was read, without a whole-run snapshot claim.
        cache_bytes += retained_bytes
        projection_sizes[cache_key] = retained_bytes
        unique_projections.add(cache_key)
        projection_cache[cache_key] = rows
        return rows

    def identity(kind, key):
        return 'n_' + digest(kind, identity_values(key))

    def key_for(row, columns):
        key = [row[c] for c in columns]
        if any(v is None or v == '' for v in key):
            raise LightGraphError('null_identity_key')
        return key

    def resolve_endpoint(mapping, row, end):
        """Resolve only matches for this input row; unrelated sets never combine."""
        nonlocal lookup_bytes, lookup_matches, lookup_omitted
        column = getattr(mapping, end + '_metadata')
        if column is not None:
            checked_column(column, allowed, goal)
            return [(physical_column_key(column), None)]
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
                token = typed_key(match)
                lookup_bytes += len(token.encode()) + 16
                check_memory('lookup_index')
                index[token].append(candidate)
            lookup_indexes[cache_key] = index
        values = [row[c] for c in columns]
        matched = lookup_indexes[cache_key].get(typed_key(values), [])
        if not matched:
            if lookup.on_missing == 'omit':
                lookup_omitted += 1
                return []
            raise LightGraphError('lookup_reference_unmatched')
        resolved = [(key_for(candidate, lookup.key_columns), candidate) for candidate in matched]
        if lookup.cardinality == 'one' and len({typed_key(key) for key, _ in resolved}) > 1:
            raise LightGraphError('lookup_reference_ambiguous')
        answers = []
        for key, candidate in resolved:
            origin = {'source_id': lookup.source_id, 'schema': lookup.schema_name, 'table': lookup.table}
            details = {'endpoint': end, 'source': origin, 'match_columns': lookup.match_columns,
                       'key_columns': lookup.key_columns, 'input_columns': columns,
                       'input_values': values, 'resolved_key': key,
                       'input_value_types': [scalar_kind(value) for value in values],
                       'resolved_key_types': [scalar_kind(value) for value in key],
                       'scope_ref': support_scope(candidate, scope_columns(lookup))}
            content = {c: candidate[c] for c in sorted(set(_projection_columns(lookup) + scope_columns(lookup)))}
            details['row_context'] = {'id': 'ctx_' + digest(origin, typed_value(content)), 'values': content,
                                     'value_types': {name: scalar_kind(value) for name, value in content.items()}}
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
            support['row_context'] = {'id': 'ctx_' + digest(origin['source'], typed_value(values)), 'values': values,
                                     'value_types': {name: scalar_kind(value) for name, value in values.items()}}
        if hasattr(mapping, 'alias_columns'):
            value = row[mapping.label_column] if mapping.label_column else None
            support['label'] = str(value) if value not in (None, '') else None
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

    def graph_properties(item, mapping, row):
        for name, value in mapping.constant_properties.items():
            accumulation.append(item, 'properties', value, property_name=name)
        for name, column in mapping.properties.items():
            value = row[column]
            if value is not None:
                accumulation.append(item, 'properties', value, property_name=name)

    definitions = {'entities': [{**e.model_dump(mode='json', exclude={'mappings'}),
                    'identity_origin': 'physical_column' if is_metadata_mapping(e.mappings[0]) else 'row'}
                    for e in draft.entities],
                   'relations': [r.model_dump(mode='json', exclude={'mappings'}) for r in draft.relations]}
    if not draft.entities:
        diagnostic('graph_draft_has_no_entities')
    try:
        clock()
        for entity in draft.entities:
            if goal is not None and goal.entity_kinds and entity.type not in goal.entity_kinds:
                diagnostic('outside_extraction_goal', entity_type=entity.type)
                continue
            for index, mapping in enumerate(entity.mappings):
                location = {'entity_type': entity.type, 'mapping': index, 'source_id': mapping.source_id}
                try:
                    clock()
                    if is_metadata_mapping(mapping):
                        node = column_node(entity, index, mapping, allowed, goal)
                        previous_node = nodes.get(node['id'])
                        if previous_node is not None:
                            previous_size = len(canonical(previous_node).encode())
                            merge_column_supports(previous_node, node)
                            update_size(previous_size, previous_node)
                        else:
                            nodes[node['id']] = node
                            update_size(0, node)
                        checked += 1
                        continue
                    if blocked_sources(mapping):
                        incomplete_entity_types.add(entity.type)
                        diagnostic('mapping_source_unavailable', **location,
                                   blocked_sources=blocked_sources(mapping))
                        continue
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
                        if label == '':
                            label = None
                        current_support = contribution(mapping, row, support)
                        node = nodes.get(node_id)
                        if node is None:
                            node = nodes[node_id] = {'id': node_id, 'type': entity.type,
                                'label': str(label) if label is not None else '',
                                'label_observed': label is not None,
                                'identity': dict(zip(entity.key_names, key)),
                                'identity_types': dict(zip(entity.key_names, map(scalar_kind, key))), 'aliases': [],
                                'properties': {}, 'provenance': [], 'supports': []}
                            accumulation.register(node)
                        if label is not None:
                            if accumulation.label_conflicts(node_id, current_support['scope_ref'], str(label)):
                                diagnostic('same_identity_conflicting_label', node_id=node_id, **location)
                            elif not node['label_observed']:
                                accumulation.replace(node, 'label', str(label))
                                accumulation.replace(node, 'label_observed', True)
                        for column in mapping.alias_columns:
                            alias = row[column]
                            if alias is not None:
                                accumulation.append(node, 'aliases', str(alias))
                        graph_properties(node, mapping, row)
                        accumulation.append(node, 'provenance', support)
                        accumulation.append(node, 'supports', current_support)
                except LightGraphError as exc:
                    incomplete_entity_types.add(entity.type)
                    failed_operation(exc)
                    if isinstance(exc, BudgetExceeded) or exc.code in {'memory_budget_exhausted', 'construction_time_exhausted', 'construction_cancelled',
                                   'run_time_budget_exhausted', 'query_budget_exhausted'}:
                        exc.construction_mapping_location = location
                        raise
                    error_diagnostic(exc, **location)
                finally:
                    rows = None
                    release_projection(mapping)
        for relation in draft.relations:
            if goal is not None and goal.relation_kinds and relation.type not in goal.relation_kinds:
                diagnostic('outside_relation_goal', relation_type=relation.type)
                continue
            pairs = set()
            for index, mapping in enumerate(relation.mappings):
                location = {'relation_type': relation.type, 'mapping': index, 'source_id': mapping.source_id}
                try:
                    clock()
                    if blocked_sources(mapping):
                        diagnostic('mapping_source_unavailable', **location,
                                   blocked_sources=blocked_sources(mapping))
                        continue
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
                    metadata_end, column_endpoint = metadata_endpoint(mapping)
                    if column_endpoint is not None:
                        support['metadata_endpoint'] = {
                            'endpoint': metadata_end,
                            'column': column_endpoint.model_dump(mode='json', by_alias=True),
                            'basis': mapping.metadata_basis,
                            'meaning_origin': 'agent_declared',
                            'row_membership': 'observed_projection',
                            'non_null_observed': mapping.metadata_basis == 'observed_non_null'}
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
                                    dependency_failed = ((source not in nodes and relation.source_type in incomplete_entity_types)
                                        or (target not in nodes and relation.target_type in incomplete_entity_types))
                                    diagnostic('relation_dependency_unavailable' if dependency_failed else
                                               'unresolved_relation_endpoint', **location)
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
                                if edge is None:
                                    edge = edges[edge_id] = {'id': edge_id, 'type': relation.type,
                                        'source': source, 'target': target, 'properties': {}, 'provenance': [], 'supports': []}
                                    accumulation.register(edge)
                                graph_properties(edge, mapping, row)
                                accumulation.append(edge, 'provenance', support)
                                current_support = contribution(mapping, row, support)
                                matches = [match for match in (source_match, target_match) if match is not None]
                                if matches:
                                    current_support['lookup_matches'] = matches
                                    current_support['required_scope_refs'] = sorted({match['scope_ref'] for match in matches})
                                    if 'row_context' not in current_support:
                                        values = {c: row[c] for c in sorted(set(_projection_columns(mapping) + scope_columns(mapping)))}
                                        current_support['row_context'] = {
                                            'id': 'ctx_' + digest(support['source'], typed_value(values)), 'values': values,
                                            'value_types': {name: scalar_kind(value) for name, value in values.items()}}
                                accumulation.append(edge, 'supports', current_support)
                except LightGraphError as exc:
                    failed_operation(exc)
                    if isinstance(exc, BudgetExceeded) or exc.code in {'memory_budget_exhausted', 'construction_time_exhausted', 'construction_cancelled',
                                   'run_time_budget_exhausted', 'query_budget_exhausted'}:
                        exc.construction_mapping_location = location
                        raise
                    error_diagnostic(exc, **location)
                finally:
                    rows = None
                    release_projection(mapping)
            if relation.acyclic and _has_cycle(pairs):
                diagnostic('relation_cycle', relation_type=relation.type)
    except LightGraphError as exc:
        error_diagnostic(exc)
        if exc.code in {'memory_budget_exhausted', 'construction_cancelled'}:
            # Do not render an over-limit or explicitly cancelled partial graph.
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
             'source_key_semantics': SOURCE_KEY_SEMANTICS_VERSION,
             'support_visibility_version': 1,
             'semantic_origin': 'agent_authored', 'purpose': draft.purpose or (goal.purpose if goal else ''),
             'definitions': definitions, 'nodes': sorted(nodes.values(), key=lambda n: n['id']),
             'edges': sorted(edges.values(), key=lambda e: e['id']), 'evidence_refs': sorted(draft_evidence_refs(draft))}
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
    # Display derivation adds fields. Do not duplicate the entire support graph
    # in memory or omit those fields from the serialized working-set boundary.
    projection_cache.clear()
    lookup_indexes.clear()
    accumulation.clear()
    rendered = derive_display_graph(graph, copy=False)
    rendered_bytes = len(canonical(rendered).encode())
    if rendered_bytes > max_working_bytes:
        diagnostic('memory_budget_exhausted', phase='display', required_bytes=rendered_bytes,
                   limit_bytes=max_working_bytes)
        rendered.update(status='candidate', nodes=[], edges=[])
        nodes.clear()
        edges.clear()
        passed = False
    return {'passed': passed, 'diagnostics': diagnostics,
            'source_read_windows': source_read_windows,
            'metrics': {'mappings_checked': checked, 'projected_distinct_rows': projected_rows,
                        'nodes': len(nodes), 'edges': len(edges), 'equal_endpoints_omitted': omitted,
                        'projection_cache_hits': cache_hits, 'unique_projections': len(unique_projections),
                        'lookup_matches': lookup_matches, 'lookup_inputs_omitted': lookup_omitted,
                        # Serialized payload plus estimated index allowances;
                        # numeric metrics deliberately do not claim actual RSS.
                        'working_memory_limit_bytes': max_working_bytes,
                        'working_memory_estimated_peak_bytes': max(peak_working_bytes, rendered_bytes),
                        'working_memory_exact_rss': False,
                        'accumulation_serialized_bytes': accumulation.serialized_bytes},
            'graph': plain_value(rendered)}
