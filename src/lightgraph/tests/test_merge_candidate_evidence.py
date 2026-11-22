"""Report the evidence behind an unresolved identity group without guessing equivalence."""
from copy import deepcopy

import pytest

from lightgraph.construction.access import filter_graph_scopes
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.tests.test_observed_identity import projection, unit


def candidate_unit(name, bindings, entity_type='Record', **options):
    graph = projection(entity_type, 'code')
    graph['nodes'] = graph['nodes'][:1]
    graph['edges'] = []
    node = graph['nodes'][0]
    original, = node['supports']
    node['supports'] = []
    for table, columns in bindings:
        support = deepcopy(original)
        support['provenance']['source']['table'] = table
        support['provenance']['key_columns'] = columns
        node['supports'].append(support)
    return unit(name, graph, **options)


@pytest.mark.parametrize('inputs,expected', [
    ([[('one', ['id']), ('two', ['id'])], [('three', ['id'])]], 'disjoint_tables'),
    ([[('one', ['id'])], [('one', ['alternate_id'])]], 'no_shared_key_binding'),
    ([[('one', ['id'])], [('one', None)]], 'incomplete_key_provenance'),
    ([[('one', ['id'])], [(None, ['id'])]], 'incomplete_source_provenance'),
    ([[('one', ['id'])], [('one', ['id'])], [('two', ['id'])]], 'partial_table_overlap'),
    ([[('one', ['id'])], [('one', ['id'])], [('one', ['part'])]], 'partial_key_binding_overlap'),
    ([[('one', ['id'])], [('one', ['id'])]], 'shared_table_and_key_columns'),
])
def test_same_typed_identity_records_physical_evidence_even_when_replay_is_disabled(inputs, expected):
    merged = merge_construction_graphs([candidate_unit(str(i), bindings) for i, bindings in enumerate(inputs)],
                                       recognize_source_replays=False)
    group, = merged['candidate_identity_merges']
    assert len(merged['nodes']) == len(inputs)
    assert group['status'] == 'candidate_only' and group['reason'] == 'cross_unit_unverified_identity_domain'
    assert (group['match_basis'], group['source_key_comparison']) == ('same_typed_identity', expected)
    visible, = filter_graph_scopes(merged, ['public'])['candidate_identity_merges']
    assert 'source_key_comparison' not in visible


def test_foreign_base_aliases_and_same_label_candidates_have_no_shared_source_authority():
    physical = [('one', ['id'])]
    merged = merge_construction_graphs([candidate_unit('base', physical, kind='base'),
                                        candidate_unit('local', physical),
                                        candidate_unit('other', physical, entity_type='Alternate')])
    exact = next(group for group in merged['candidate_identity_merges']
                 if group['reason'] == 'cross_unit_unverified_identity_domain')
    assert exact['source_key_comparison'] == 'unverified_source_namespace'
    homonym = next(group for group in merged['candidate_identity_merges'] if 'normalized_label' in group)
    assert homonym['match_basis'] == 'normalized_label' and homonym['status'] == 'candidate_only'
    assert 'source_key_comparison' not in homonym


def test_common_key_columns_do_not_prove_shared_context():
    first = candidate_unit('a', [('table', ['item_id'])])
    second = candidate_unit('b', [('table', ['item_id'])])
    second['graph']['nodes'][0]['supports'][0]['business_context']['dimensions']['project'] = 'K'
    merged = merge_construction_graphs([first, second])
    group, = merged['candidate_identity_merges']
    assert len(merged['nodes']) == 2 and group['status'] == 'candidate_only'
    assert group['source_key_comparison'] == 'shared_table_and_key_columns'


def test_same_contract_in_a_split_family_still_requires_identity_evidence_across_tables():
    first = candidate_unit('first', [('one', ['id']), ('two', ['id'])])
    second = candidate_unit('second', [('three', ['id'])])
    composite = candidate_unit('composite', [('other', ['id', 'title'])])
    graph = composite['graph']
    graph['definitions']['entities'][0]['key_names'].append('name')
    graph['nodes'][0]['identity']['name'] = 'Example'
    graph['nodes'][0]['identity_types']['name'] = 'text'
    merged = merge_construction_graphs([first, second, composite])
    exact, = [group for group in merged['candidate_identity_merges']
              if group['match_basis'] == 'same_typed_identity']
    assert merged['fusion_diagnostics']['source_replay_status'] == 'ran'
    assert merged['fusion_diagnostics']['source_replay_collapsed_nodes'] == 0
    assert exact['source_key_comparison'] == 'disjoint_tables'
    # Both single-key definitions receive the same variant type. Contributor
    # scoping then keeps those two nodes apart pending a shared domain.
    nodes = [node for node in merged['nodes'] if node['id'] in exact['node_ids']]
    assert {node['type'] for node in nodes} == {exact['entity_type']}
    assert len({node['unit_original_id'] for node in nodes}) == 1
    assert {node['unit_id'] for node in nodes} == {'first', 'second'}
