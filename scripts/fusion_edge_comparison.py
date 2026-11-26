"""Compare committed relation observations across extractions using their frozen source evidence."""
from collections import Counter, defaultdict
from dataclasses import dataclass

from lightgraph.construction.checkpoints import load_checkpoint
from lightgraph.construction.fusion import definition_contract
from lightgraph.construction.hierarchy_replay import _row_matches_endpoint
from lightgraph.construction.merge_name_audit import _typed_key
from lightgraph.construction.relation_assertions import assertion_signature, support_address
from lightgraph.construction.source_namespaces import BINDING
from lightgraph.construction.workflow import run_output_path
from lightgraph.contracts import digest

from audit_checkpoint_merge import archived_state
from fusion_census_nodes import _physical_values


@dataclass
class _Endpoint:
    entity_type: str
    cells: tuple
    raw_key: tuple
    bindings: dict
    meaning: tuple


def _node_row_agrees(row, columns, raw_key):
    return (isinstance(row, dict) and isinstance(row.get('values', {}), dict)
            and isinstance(row.get('value_types', {}), dict) and _row_matches_endpoint(row, columns, raw_key))


def _index_endpoint(node, definition):
    names = definition.get('key_names') or []
    key = _typed_key(node, definition)
    if not key:
        return None
    ordered = [key['fields'][name] for name in names] if 'fields' in key else [key]
    cells = tuple((value['value_type'], value['value']) for value in ordered)
    raw_key = tuple((node['identity_types'][name], node['identity'][name]) for name in names)
    bindings = {}
    for support in node.get('supports') or []:
        if not isinstance(support, dict) or not isinstance(support.get('provenance'), dict):
            continue
        columns = support['provenance'].get('key_columns')
        address = support_address(support)
        if (not address or not isinstance(columns, list) or len(columns) != len(names)
                or not all(isinstance(c, str) and c for c in columns) or len(set(columns)) != len(columns)):
            continue
        binding = (address, tuple(columns))
        # A contradiction on this projection cannot be repaired by another support
        # carrying the same column names. Other projections remain independent.
        valid = (not support.get('inherited_from')
                 and _physical_values(node, support, columns, names) == cells
                 and _node_row_agrees(support.get('row_context') or {}, columns, raw_key))
        bindings[binding] = bindings.get(binding, True) and valid
    meaning = (definition_contract(definition), definition.get('description'),
               node.get('identity_context'), node.get('node_role'))
    return _Endpoint(node['type'], cells, raw_key, bindings, meaning)


def _endpoint_attested(support, side, endpoint):
    columns = support['provenance'][side + '_key_columns']
    return (endpoint.bindings.get((support_address(support), tuple(columns)), False)
            and _row_matches_endpoint(support.get('row_context') or {}, columns, endpoint.raw_key))


def _index(graph):
    entities = {d['type']: d for d in graph['definitions']['entities']}
    relations = {d['type']: d for d in graph['definitions']['relations']}
    endpoints = {}
    for n in graph['nodes']:
        if n['id'] in endpoints:
            raise ValueError('节点 ID 不唯一，无法核对边端点。')
        endpoints[n['id']] = _index_endpoint(n, entities.get(n['type'], {}))
    groups = defaultdict(lambda: defaultdict(list))
    unverified = defaultdict(list)
    seen = set()
    for edge in graph['edges']:
        eid = edge['id']
        if eid in seen:
            raise ValueError('关系 ID 不唯一，无法逐边对账。')
        seen.add(eid)
        definition = relations.get(edge['type'])
        reason, witness, _ = assertion_signature(edge, definition)
        source = endpoints.get(edge['source'])
        target = endpoints.get(edge['target'])
        if not reason:
            if source is None or target is None:
                reason = 'missing_typed_endpoint'
            elif source.entity_type != definition.get('source_type') or target.entity_type != definition.get('target_type'):
                reason = 'inconsistent_endpoint_type'
            elif any(not _endpoint_attested(support, side, endpoint)
                     for support in edge['supports'] for side, endpoint in (('source', source), ('target', target))):
                reason = 'unverified_physical_endpoint'
        if reason:
            unverified[reason].append(eid)
            continue
        # The witness covers physical columns, snapshot, row intervals and qualifications.
        # Keep the declared meaning separate so that a renamed assertion remains reviewable.
        observed = digest(source.cells, target.cells, witness)
        meaning = digest(definition_contract(definition), definition.get('description'), [source.meaning, target.meaning])
        groups[observed][meaning].append(eid)
    return len(seen), groups, unverified


def _load(run_file, node):
    run, project, state = archived_state(run_file)
    checkpoint = state['artifacts'][node]
    if checkpoint.get('artifact_status') != 'committed':
        raise ValueError('请提供已提交检查点。')
    directory = run_output_path(project, state.get('artifact_outputs', {}).get(node, run['output_path']))
    graph = load_checkpoint(directory, checkpoint)['graph']
    namespace = digest('project_source_namespace_v1', run.get('project_id'))
    if not run.get('project_id') or graph.get(BINDING, namespace) != namespace:
        raise ValueError('检查点来源命名空间无法与运行项目对齐。')
    return {'project_id': run['project_id'], 'run_id': run['run_id'], 'node_id': node,
            'checkpoint_run_id': checkpoint['run_id'], 'artifact_id': checkpoint['artifact_id'],
            'graph_digest': checkpoint['graph_digest']}, _index(graph)


def compare_source_relations(old_run, new_run, node):
    old, first = _load(old_run, node)
    new, second = _load(new_run, node)
    if old['project_id'] != new['project_id']:
        raise ValueError('请选择同一项目来源命名空间的两次运行。')
    counts = [Counter(), Counter()]
    groups = []
    for physical in sorted(first[1].keys() | second[1].keys()):
        left, right = first[1].get(physical, {}), second[1].get(physical, {})
        for meaning in sorted(left.keys() | right.keys()):
            ids = [sorted(left.get(meaning, [])), sorted(right.get(meaning, []))]
            if all(ids):
                status = 'same_qualified_observation_and_contract'
            elif left and right:
                status = 'same_qualified_observation_other_contract'
            else:
                status = 'observed_on_one_side_only'
            for tally, members in zip(counts, ids, strict=True):
                tally[status] += len(members)
            groups.append({'status': status, 'observation_digest': physical, 'contract_digest': meaning,
                           'old_edge_ids': ids[0], 'new_edge_ids': ids[1]})

    def summarize(index, tally):
        raw, _, unverified = index
        reasons = {reason: sorted(edges) for reason, edges in sorted(unverified.items())}
        tally['insufficient_evidence_or_self_loop'] = sum(map(len, reasons.values()))
        if sum(tally.values()) != raw:
            raise ValueError('原始关系条数未闭合。')
        return {'raw_edges': raw, 'raw_edges_by_status': dict(tally),
                'unverified_counts_by_reason': {reason: len(edges) for reason, edges in reasons.items()},
                'unverified_edge_ids_by_reason': reasons}

    return {'basis': 'committed_qualified_worksheet_observations', 'old': {**old, **summarize(first, counts[0])},
            'new': {**new, **summarize(second, counts[1])}, 'groups': groups}
