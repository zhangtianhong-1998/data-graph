"""Reconcile independently published drafts even when they share one extraction checkpoint."""
from collections import Counter
from copy import deepcopy

import pytest

from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.tests.test_hierarchy_projection_variants import _facts
from lightgraph.tests.test_source_hierarchy_replays import code, contributor, draft_graph
from lightgraph.tests.test_web_graph_review import client
from lightgraph.tests.test_web_publication_identity import GRAPH, listing, publish, revoke, submit


def _checkpoint():
    return contributor('extract_1', draft_graph(1, 'Account'), draft_graph(2, 'Account', inverse=True))


def _change_origin(unit, change):
    for key in ('nodes', 'edges'):
        for item in unit['graph'][key]:
            for support in item['supports']:
                for origin in support['execution_origins']:
                    if origin['draft_id'] == 'draft2':
                        if change == 'unversioned':
                            origin.pop('draft_revision')
                        elif change == 'same_author':
                            origin.update(draft_id='draft1', draft_revision=2)
                        elif change == 'borrowed_edges' and key == 'edges':
                            origin['draft_id'] = 'unrelated'


def test_two_authored_variants_of_one_unit_retain_original_observations_and_directions():
    unit = _checkpoint()
    original = deepcopy(unit)
    control = merge_construction_graphs([unit], recognize_source_replays=False)
    graph = merge_construction_graphs([unit])
    assert original == unit
    assert len(control['nodes']) == 6 and len(graph['nodes']) == 3
    assert len(graph['edges']) == len(control['edges']) == 4
    assert _facts(graph) == _facts(control)
    stats = graph['fusion_diagnostics']['hierarchy_replay_diagnostics']
    assert stats['within_unit_physical_sets'] == 3 and stats['cross_unit_physical_sets'] == 0
    assert stats['collapsed_nodes_by_role_evidence'] == {'same_unit_versioned_drafts': 3}
    assert not graph['candidate_identity_merges']


@pytest.mark.parametrize('change', ['unversioned', 'same_author', 'borrowed_edges'])
@pytest.mark.parametrize('third', [False, True])
def test_another_unit_cannot_validate_unattributed_or_nonindependent_siblings(change, third):
    unit = _checkpoint()
    _change_origin(unit, change)
    inputs = [unit, contributor('extract_2', draft_graph(3, 'Other'))] if third else [unit]
    graph = merge_construction_graphs(inputs)
    assert Counter(code(n) for n in graph['nodes']) == {'A-01': len(inputs) + 1, 'B-01': len(inputs) + 1,
                                                         'X': len(inputs) + 1}


def test_review_by_full_key_in_one_published_unit_ignores_another_code_with_the_same_label(client):
    unit = _checkpoint()
    _change_origin(unit, 'unversioned')
    graph = publish(client, [unit])
    before = {n['id']: code(n) for n in graph['nodes']}
    view = listing(client)
    assert len(view['candidates']) == 3
    for candidate in view['candidates']:
        assert len({before[n] for n in candidate['members']}) == 1
        assert candidate['reason'] == 'hierarchy_source_key_requires_review'
    chosen = next(c for c in view['candidates'] if before[c['members'][0]] == 'A-01')
    decision = submit(client, view, chosen)
    assert decision.status_code == 200, decision.text
    after = client.get(GRAPH + '/export').json()
    ids = {n['id'] for n in after['entities']}
    assert len(ids.intersection(chosen['members'])) == 1
    assert ids - set(chosen['members']) == before.keys() - set(chosen['members'])
    assert len(after['relationships']) == len(graph['edges'])
    assert revoke(client, listing(client), decision.json()).status_code == 200
    assert {n['id'] for n in client.get(GRAPH + '/export').json()['entities']} == before.keys()


def test_a_candidate_cannot_reveal_a_shared_private_binding_or_ignore_the_policy():
    unit = _checkpoint()
    _change_origin(unit, 'unversioned')
    assert not merge_construction_graphs([unit], merge_policy={'emit_alignment_candidates': False})['candidate_identity_merges']
    for node in unit['graph']['nodes']:
        if node['identity_domain'] == 'namespace 2':
            node['supports'][0]['required_scope_refs'] = ['private']
    graph = merge_construction_graphs([unit])
    assert len(graph['nodes']) == 6
    assert not graph['candidate_identity_merges']
