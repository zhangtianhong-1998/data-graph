"""Recheck a deterministic hierarchy merge against the exact original unit graphs."""
from copy import deepcopy

from lightgraph.contracts import digest
from .display import derive_display_graph
from .fusion import deduplicate_supports
from .graph_storage import _graph_digest
from .source_namespaces import source_namespace


RECORD = 'hierarchy_replay_inputs'


def _policy_key(policy):
    # Supplying conflict_mode explicitly changes the default label policy.
    return digest(policy.model_dump(mode='json'), sorted(policy.model_fields_set))


def _clean_unit(unit):
    graph = unit['graph']
    return (isinstance(unit.get('contributor_id'), str) and unit.get('kind', 'unit') == 'unit'
            and not graph.get('extends_base_graph')
            and not unit.get('inherited_grants') and not graph.get('inherited_grants')
            and not graph.get(RECORD) and 'fusion_diagnostics' not in graph
            and (graph.get('conflict_isolation') or {}).get('coverage') != 'incomplete'
            and not any(s.get('inherited_from') or s.get('lookup_matches')
                        for field in ('nodes', 'edges') for item in graph.get(field, [])
                        for s in item.get('supports', [])))


def _evidence(unit):
    # Only these catalog fields are consumed by the merger. Probe result rows are never copied.
    result = {}
    for ref, record in (unit.get('evidence_records') or {}).items():
        entry = {k: record[k] for k in ('kind', 'source_ref') if k in record}
        if record.get('kind') == 'source_probe' and record.get('details'):
            entry['details'] = {k: record['details'][k] for k in ('operation', 'complete') if k in record['details']}
        result[ref] = entry
    return result


def _unit_key(unit):
    return digest(unit['contributor_id'], _graph_digest(unit['graph']), _evidence(unit))


def _catalog_header(unit):
    """Only retain declarations and scalar format fields for the reversible self-hierarchy shape."""
    graph = unit['graph']
    fields = {'format_version', 'target', 'status', 'semantic_origin', 'definitions', 'evidence_refs',
              'source_key_semantics', 'support_visibility_version', 'label_conflict_policy', 'access_fingerprint',
              'definition_conflicts', 'unresolved_relations', 'context_cycle_diagnostics'}
    definitions = graph.get('definitions', {})
    if (set(graph) - fields - {'nodes', 'edges'} or not definitions.get('relations')
            or any(graph.get(k) for k in ('definition_conflicts', 'unresolved_relations', 'context_cycle_diagnostics'))
            or any(r.get('hierarchy_orientation') not in ('parent_to_child', 'child_to_parent')
                   or r.get('source_type') != r.get('target_type') for r in definitions['relations'])):
        return None
    return {k: deepcopy(v) for k, v in graph.items() if k in fields}


def _stage_key(graph, record):
    content = {k: v for k, v in graph.items() if k != RECORD}
    # write_sharded_graph adds this exact envelope field after committing the merge.
    if content.get('storage_layout') == 'parquet_v1':
        del content['storage_layout']
    return digest(_graph_digest(content),
                  {k: v for k, v in record.items() if k != 'stage_digest'})


def record_stage(graph, inputs, policy):
    namespace = source_namespace(inputs)
    attempted = graph['fusion_diagnostics'].get('hierarchy_replay_diagnostics') or {}
    if (namespace is None or not attempted.get('verified_source_node_inputs')
            or not any(r.get('source_type') == r.get('target_type') and r.get('hierarchy_orientation') != 'none'
                       for unit in inputs for r in unit['graph'].get('definitions', {}).get('relations', ()))
            or graph['conflict_isolation']['coverage'] != 'complete' or not all(_clean_unit(u) for u in inputs)):
        return
    units = {}
    for unit in inputs:
        entry = {'digest': _unit_key(unit)}
        header = _catalog_header(unit)
        if header is not None:
            entry.update(header=header, evidence_records=_evidence(unit))
        units[unit['contributor_id']] = entry
    record = {'version': 1, 'source_namespace': namespace, 'policy': _policy_key(policy), 'units': units}
    record['stage_digest'] = _stage_key(graph, record)
    graph[RECORD] = record


def _raw_support(support, execution, cid, catalog):
    """Restore the execution-specific mapping and evidence references of this physical observation."""
    def original_refs():
        refs = []
        for ref in execution.get('evidence_refs', []):
            entry = catalog.get(ref, {})
            if entry.get('contributor_id') != cid or entry.get('contributor_kind') != 'unit':
                raise ValueError('non-local observation evidence')
            refs.append(entry['original_ref'])
        return refs

    raw = {k: deepcopy(v) for k, v in support.items() if k not in {
        'contributor_id', 'observation_id', 'execution_origins', 'identity_basis', 'original_key_values',
        'identity_origins', 'endpoint_origins', 'label_priority', 'label_authority'}}
    provenance = raw['provenance']
    for k in ('definition', 'mapping'):
        provenance[k] = execution[k]
    origin = {k: v for k, v in execution.items() if k != 'contributor_id'}
    for target in (raw, provenance, origin):
        if 'evidence_refs' in target:
            target['evidence_refs'] = original_refs()
    raw['execution_origins'] = [origin]
    return raw


def _original_item(item, support, execution, field):
    cid, definition = execution['contributor_id'], execution['definition']
    if field == 'nodes':
        if support.get('identity_basis') != 'observed_hierarchy_projection_v1':
            raise ValueError('missing source identity')
        matches = [i for i in support['identity_origins'] if i.get('contributor_id') == cid
                   and i.get('type') == definition]
        origin, = matches
        return {**{k: deepcopy(v) for k, v in origin.items() if k != 'contributor_id'},
                'identity_context': item['identity_context'], 'aliases': []}
    origins = support.get('endpoint_origins', [])
    if origins:
        if isinstance(origins, dict):
            origins = [origins]
        matches = [i for i in origins if i.get('input_kind') == 'unit' and i.get('contributor_id') == cid]
        origin, = matches or [i for i in origins if 'input_kind' not in i and i.get('type') == definition]
        shell = {'id': origin['edge_id'], 'source': origin['source'], 'target': origin['target'], 'type': origin['type']}
    else:
        if item['type'] != definition:
            raise ValueError('missing source endpoints')
        shell = {k: item[k] for k in ('id', 'type', 'source', 'target')}
    if 'business_context' in item:
        shell['business_context'] = item['business_context']
    return shell


def _restore_units(graph, declarations, namespace):
    """Recover solely from published observations. Even a single missing field invalidates the unit digest."""
    units = {cid: {'contributor_id': cid, 'source_namespace': namespace, 'graph': deepcopy(entry['header']),
                   'evidence_records': entry['evidence_records']} for cid, entry in declarations.items()}
    for field in ('nodes', 'edges'):
        items = {cid: {} for cid in units}
        for item in graph[field]:
            for support in item['supports']:
                for execution in support['execution_origins']:
                    cid = execution['contributor_id']
                    shell = _original_item(item, support, execution, field)
                    raw = _raw_support(support, execution, cid, graph['evidence_catalog'])
                    original = items[cid].setdefault(shell['id'], dict(shell, properties={}, provenance=[], supports=[]))
                    original['supports'].append(raw)
                    if raw['provenance'] not in original['provenance']:
                        original['provenance'].append(raw['provenance'])
                    for name, values in raw.get('properties', {}).items():
                        collected = original['properties'].setdefault(name, [])
                        collected.extend(v for v in values if v not in collected)
                    if field == 'nodes':
                        original['aliases'].extend(a for a in raw.get('aliases', []) if a not in original['aliases'])
        for cid, by_id in items.items():
            for item in by_id.values():
                deduplicate_supports(item)
            units[cid]['graph'][field] = [by_id[k] for k in sorted(by_id)]
    for unit in units.values():
        derive_display_graph(unit['graph'], copy=False)
    return list(units.values())


def original_stage_inputs(inputs, policy, target_access_fingerprint):
    """Flatten only if every supplied stage and every recovered unit matches its recorded digest."""
    namespace = source_namespace(inputs)
    if not namespace or len(inputs) < 2 or not any(c.get('kind') == 'merged' for c in inputs):
        return inputs
    if len({(c.get('kind', 'unit'), str(c['contributor_id'])) for c in inputs}) != len(inputs):
        return inputs  # The merger reports duplicate direct contributors.
    leaves = {}
    for c in inputs:
        graph = c['graph']
        if c.get('kind') == 'merged':
            record = graph.get(RECORD)
            if (not isinstance(record, dict) or record.get('version') != 1 or c.get('inherited_grants')
                    or graph.get('access_fingerprint') != target_access_fingerprint
                    or record.get('source_namespace') != namespace or record.get('policy') != _policy_key(policy)
                    or record.get('stage_digest') != _stage_key(graph, record)):
                return inputs
            entries = record['units']
            originals = c.get('original_units')
            if originals is None:
                try:
                    originals = _restore_units(graph, entries, namespace)
                except (KeyError, TypeError, ValueError):
                    return inputs
            if (not isinstance(originals, list) or source_namespace(originals) != namespace
                    or len(originals) != len(entries)
                    or any(not isinstance(u.get('graph'), dict) or not isinstance(u.get('contributor_id'), str)
                           for u in originals)
                    or {u['contributor_id'] for u in originals} != set(entries)):
                return inputs
            if any(not _clean_unit(u) or _unit_key(u) != entries[u['contributor_id']]['digest'] for u in originals):
                return inputs
        elif _clean_unit(c):
            originals = [c]
        else:
            return inputs
        for unit in originals:
            cid = unit['contributor_id']
            if cid in leaves and _unit_key(leaves[cid]) != _unit_key(unit):
                return inputs
            leaves[cid] = unit
    return [leaves[cid] for cid in sorted(leaves)]
