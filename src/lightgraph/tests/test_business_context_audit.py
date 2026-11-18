"""Counterexamples from independent review of qualified graph semantics."""
from copy import deepcopy

from lightgraph.construction.context_topology import detect_context_cycles
from lightgraph.tests.test_construction_business_context import fixture, execute


def edge(name, source, target, context=None):
    support = {'scope_ref': 'public'}
    if context is not None:
        support['business_context'] = {
            'status': 'global' if not context else 'scoped', 'dimensions': context}
    return {'id': name, 'source': source, 'target': target, 'supports': [support]}


def test_executor_does_not_invent_a_cycle_across_projects():
    table, draft = fixture()
    parent = deepcopy(draft['entities'][0]['mappings'][0])
    parent['key_columns'] = ['parent']
    parent['properties'] = {}
    draft['entities'][0]['mappings'].append(parent)
    draft['relations'] = [{'type': 'parent', 'source_type': 'Item', 'target_type': 'Item', 'acyclic': True,
        'mappings': [{'source_id': 'C', 'table': 'items', 'source_key_columns': ['code'],
                      'target_key_columns': ['parent'],
                      'business_context': {'status': 'scoped', 'columns': {'project': 'project'}}}]}]
    result = execute(table, draft, [('M', 'A', 'B', 1, 'Q1'), ('K', 'B', 'A', 2, 'Q1')])
    assert result['passed'], result['diagnostics']
    assert len(result['graph']['edges']) == 2
    real_cycle = execute(table, draft, [('M', 'A', 'B', 1, 'Q1'), ('M', 'B', 'A', 2, 'Q1')])
    assert not real_cycle['passed']
    assert any(item['code'] == 'relation_cycle' for item in real_cycle['diagnostics'])


def test_global_edges_participate_in_project_cycles_but_unknown_edges_do_not():
    scoped = edge('scoped', 'A', 'B', {'project': 'M'})
    global_cycle = detect_context_cycles([scoped, edge('global', 'B', 'A', {})])
    assert global_cycle['complete'] and global_cycle['cycle_edge_ids'] == ['global', 'scoped']
    unknown = detect_context_cycles([scoped, edge('unknown', 'B', 'A')])
    assert unknown['complete'] and unknown['cycle_edge_ids'] == []
    assert unknown['unknown_context_edge_count'] == 1
    legacy = detect_context_cycles([edge('a', 'A', 'B'), edge('b', 'B', 'A')])
    assert legacy['cycle_edge_ids'] == ['a', 'b']


def test_time_and_role_dimensions_are_conjoined_along_paths():
    disjoint = [edge('q1', 'A', 'B', {'project': 'M', 'period': 'Q1'}),
                edge('q2', 'B', 'A', {'project': 'M', 'period': 'Q2'})]
    assert detect_context_cycles(disjoint)['cycle_edge_ids'] == []
    compatible = [edge('project', 'A', 'B', {'project': 'M'}),
                  edge('time', 'B', 'C', {'period': 'Q1'}),
                  edge('both', 'C', 'A', {'project': 'M', 'period': 'Q1'})]
    assert detect_context_cycles(compatible)['cycle_edge_ids'] == ['both', 'project', 'time']
    compatible.append(edge('other', 'B', 'A', {'project': 'K'}))
    assert detect_context_cycles(compatible)['cycle_edge_ids'] == ['both', 'project', 'time']


def test_cycle_budget_exhaustion_is_explicitly_inconclusive():
    mixed = [edge('m1', 'A', 'B', {'project': 'M'}), edge('k1', 'B', 'A', {'project': 'K'}),
             edge('m2', 'B', 'C', {'project': 'M'}), edge('m3', 'C', 'A', {'project': 'M'})]
    check = detect_context_cycles(mixed, max_states=1)
    assert not check['complete'] and check['incomplete_reason'] == 'state_limit'
    memory_check = detect_context_cycles(mixed, max_state_bytes=1)
    assert not memory_check['complete'] and memory_check['incomplete_reason'] == 'state_memory_limit'
    assert detect_context_cycles(mixed)['cycle_edge_ids'] == ['m1', 'm2', 'm3']


def test_cycle_analysis_uses_support_context_instead_of_aggregated_header():
    a = edge('a', 'A', 'B', {'project': 'M'})
    b = edge('b', 'B', 'A', {'project': 'K'})
    a['business_context'] = b['business_context'] = {'status': 'global', 'dimensions': {}}
    assert detect_context_cycles([a, b])['cycle_edge_ids'] == []
