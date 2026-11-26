"""A finite proposal may complete a proven group without taking credit for its existing members."""
from copy import deepcopy
from pathlib import Path
import runpy

import pytest

from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.construction.overlap_patterns import analyze_overlap_patterns
from lightgraph.construction.projection_proposals import verify_projection_proposals
from lightgraph.contracts import digest
from lightgraph.runtime.config import MergePolicyConfig
from lightgraph.tests.test_batch_overlap_patterns import unit
from lightgraph.tests.test_projection_proposals import _batch, _bind_section, _compile
from lightgraph.tests.test_source_hierarchy_replays import contributor, draft_graph


def hierarchy_inputs():
    inputs = []
    for number, name in enumerate(('Alpha', 'Beta', 'Gamma'), 1):
        graph = draft_graph(number, name, project='P')
        graph['definitions']['entities'][0]['identity_domain'] = 'common'
        for node in graph['nodes']:
            node['identity_domain'] = 'common'
        branch = contributor(name, graph, evidence_kind='document' if name == 'Gamma' else 'source_probe')
        branch.update(kind='unit', source_namespace='ws')
        branch['graph']['access_fingerprint'] = 'authorized'
        branch['graph']['definitions']['relations'][0]['original_type'] = 'parent_link'
        inputs.append(branch)
    return inputs


def leaf_policy(inputs, selected):
    card = next(p for p in analyze_overlap_patterns(inputs).patterns if p['kind'] == 'entity'
                and p['basis'] == 'same_physical_key')
    alignment = []
    for i, member in enumerate(card['members']):
        if member['contributor_id'] in selected:
            source = next(j for j in member['source_indexes'] if card['sources'][j]['key_columns'] == ['leaf'])
            role = next(j for j, r in enumerate(member['roles']) if r['end'] == 'target')
            alignment.append({'member_index': i, 'role_index': role, 'source_index': source})
    policy, (report,) = verify_projection_proposals(MergePolicyConfig(), inputs, [card], [
        {'action': 'propose', 'pattern_id': card['pattern_id'], 'alignment': alignment}])
    assert report['verified_groups'] == 2
    return policy


@pytest.mark.parametrize('all_members', [True, False])
def test_proposed_leaf_group_only_extends_a_fully_covered_hierarchy_pair(all_members):
    inputs = hierarchy_inputs()
    control = merge_construction_graphs(inputs, recognize_source_replays=False)
    base = merge_construction_graphs(inputs)
    selected = {'Alpha', 'Beta', 'Gamma'} if all_members else {'Alpha', 'Gamma'}
    policy = leaf_policy(inputs, selected)
    actual = merge_construction_graphs(inputs, merge_policy=policy)
    assert actual == merge_construction_graphs(list(reversed(inputs)), merge_policy=policy)
    stats = actual['fusion_diagnostics']
    assert stats['hierarchy_replay_collapsed_nodes'] == 3 == base['fusion_diagnostics']['hierarchy_replay_collapsed_nodes']
    assert stats['model_verified_projections']['collapsed_nodes'] == (2 if all_members else 0)
    assert len(base['nodes']) == 6 and len(actual['nodes']) == (4 if all_members else 6)
    assert len(actual['edges']) == len(base['edges']) == 6
    audit = runpy.run_path(str(Path(__file__).resolve().parents[3] / 'scripts' / 'audit_checkpoint_merge.py'))
    control_ids = {n['id'] for n in control['nodes']}
    membership = audit['hierarchy_membership'](actual, control_ids)
    assert membership == audit['hierarchy_membership'](base, control_ids)
    assert membership['agrees_with_recorded_count']
    if all_members:
        leafs = [n for n in actual['nodes'] if n['label'] == 'Inscription']
        assert len(leafs) == 2
        for node in leafs:
            code, = node['identity'].values()
            expected = {(i['contributor_id'], n['id']) for i in inputs for n in i['graph']['nodes']
                        if code in n['identity'].values()}
            assert {(s['contributor_id'], s['reviewed_input_node_id']) for s in node['supports']} == expected
        assert all(all(s['identity_decision_endpoints']['target'] for s in e['supports']) for e in actual['edges'])
    else:
        assert actual['nodes'] == base['nodes'] and actual['edges'] == base['edges']


def test_prejoined_unit_ids_must_be_exhaustively_owned_and_unreviewed():
    branches = [unit('a', 'Region', 'code', field='size'), unit('b', 'Region', 'code', field='length'),
                unit('c', 'Other', 'logical', field='language', role='other')]
    first, second, third = branches
    third['graph']['definitions']['relations'][0]['original_type'] = first['graph']['edges'][0]['type']
    for branch in branches:
        _bind_section(branch, 'v1')
        branch['graph']['definitions']['entities'][0]['identity_evidence_refs'] = ['e1']
    for a, b in zip(first['graph']['nodes'], second['graph']['nodes'], strict=True):
        b['id'] = a['id']
    second['graph']['edges'][0].update(source='a_0', target='a_1')
    policy, _ = _compile(branches)
    base = merge_construction_graphs(branches)
    assert len(base['nodes']) == 4
    graph = merge_construction_graphs(branches, merge_policy=policy)
    assert len(graph['nodes']) == 3 and len(graph['edges']) == len(base['edges'])
    joined = next(n for n in graph['nodes'] if n['id'] == 'a_0')
    assert {(s['contributor_id'], s['reviewed_input_node_id']) for s in joined['supports']} == {
        ('a', 'a_0'), ('b', 'a_0'), ('c', 'c_0')}
    assert graph['fusion_diagnostics']['model_verified_projections']['collapsed_nodes'] == 1

    partial_policy, _ = _compile([first, third])
    blocked = merge_construction_graphs(branches, merge_policy=partial_policy)
    assert blocked['edges'] == base['edges'] and blocked['nodes'] == base['nodes']
    marked = deepcopy(branches)
    marked[0]['graph']['identity_separations'] = [{'members': ['a_0', 'a_1']}]
    marked_policy, _ = _compile(marked)
    blocked = merge_construction_graphs(marked, merge_policy=marked_policy)
    assert blocked['fusion_diagnostics']['model_verified_projections']['collapsed_nodes'] == 0


@pytest.mark.parametrize(('case', 'policy_digest', 'rows_digest'), [
    ('disjoint', 'dff7d6e32b4ae9733eaef4b7952d3fc3c39077354d5bba68dfddf3d7e0187fc3',
     '5635840af7a5c90da2eda8ef3ef71696b976c8c1d4c6997b9be66db7c97aa3b2'),
    ('hierarchy', '87d8533b571b5718a9c57a4a02bcff11f225affd430a88b348bcdb256e14c111',
     'ffcc888a9aa8d4452deb06ac7a6a04bfa932b59fe59be7a9eec63676a98c4b6d'),
])
def test_archived_finite_rules_reproduce_the_policy_and_full_output_from_734d6e9(case, policy_digest, rows_digest):
    inputs = hierarchy_inputs() if case == 'hierarchy' else _batch()
    current = leaf_policy(inputs, {'Alpha', 'Beta', 'Gamma'}) if case == 'hierarchy' else _compile(inputs)[0]
    archive = current.model_dump(mode='json')
    assert archive['verified_projection_rules'][0].pop('composition_version') == 3
    legacy = MergePolicyConfig.model_validate(archive)
    assert legacy.model_dump(mode='json') == archive
    replay = merge_construction_graphs(inputs, merge_policy=legacy)
    assert digest(archive) == policy_digest
    assert digest(replay) == rows_digest  # Includes members, original edge lineage, diagnostics and stage metadata.
    stats = replay['fusion_diagnostics']
    assert stats['hierarchy_replay_collapsed_nodes'] == (3 if case == 'hierarchy' else 0)
    assert stats['model_verified_projections']['collapsed_nodes'] == (0 if case == 'hierarchy' else 1)


@pytest.mark.parametrize('overlap', [False, True])
def test_endpoint_collision_cannot_undo_an_existing_hierarchy_pair(monkeypatch, overlap):
    """Exercise topology at the stage after both independent member proofs."""
    from collections import Counter, defaultdict
    from lightgraph.construction import hierarchy_replay
    from lightgraph.construction.projection_proposals import ProjectionInputs

    a = unit('A', 'Kind', 'key')
    base = merge_construction_graphs([a], recognize_source_replays=False)
    shell = base['nodes'][0]
    base['nodes'] = [{**deepcopy(shell), 'id': name} for name in ('1', '2', '3', '4')]
    edge_shell = base['edges'][0]
    # No duplicate under the old pair alone, nor under the disjoint proposal
    # alone. Accepting both would make these two edges identical.
    base['edges'] = [{**deepcopy(edge_shell), 'id': 'edge'+left, 'source': left, 'target': right}
                     for left, right in [('1', '3'), ('2', '4')]]
    if overlap:
        base['edges'] = [{**deepcopy(edge_shell), 'id': 'e', 'source': '3', 'target': '1'}]

    monkeypatch.setattr(hierarchy_replay, '_verified_groups', lambda *args, **kw: [(('1', '2'), 'direct_neighbors')])
    baseline = deepcopy(base)
    assert hierarchy_replay.reconcile_hierarchy_projections(baseline) == 1
    ids = ('1', '2', '3') if overlap else ('3', '4')
    refs = ProjectionInputs({i: {'A': i} for i in ids}, True)
    stats = {'not_applied_by_pattern': defaultdict(Counter)}
    assert hierarchy_replay.reconcile_hierarchy_projections(
        base, model_groups={ids: (None, 'test', refs)}, projection_stats=stats) == 1
    assert base == baseline
    assert stats['collapsed_nodes'] == 0 and stats['not_applied_by_pattern']['test']['incompatible_endpoint_remap'] == 1


def test_evidence_for_ab_and_bc_does_not_authorize_abc():
    inputs = [unit(owner, kind, 'code', field=owner) for owner, kind in
              [('a', 'District'), ('b', 'Area'), ('c', 'Zone')]]
    left, _ = _compile(inputs[:2])
    right, _ = _compile(inputs[1:])
    policy = MergePolicyConfig(verified_projection_rules=[*left.verified_projection_rules, *right.verified_projection_rules])
    base = merge_construction_graphs(inputs)
    actual = merge_construction_graphs(inputs, merge_policy=policy)
    assert (actual['nodes'], actual['edges']) == (base['nodes'], base['edges'])
    stats = actual['fusion_diagnostics']['model_verified_projections']
    assert stats['applied_groups'] == 0
    assert list(stats['not_applied_by_pattern'].values()) == [{'overlapping_proposals': 1}] * 2


def test_more_than_32_verified_pattern_rules_can_be_reloaded_for_replay():
    inputs = _batch()
    policy, _ = _compile(inputs)
    first, = policy.verified_projection_rules
    expanded = policy.model_copy(update={'verified_projection_rules': [
        first.model_copy(update={'pattern_id': f'pattern_{i:024x}'}) for i in range(33)]})
    reloaded = MergePolicyConfig.model_validate_json(expanded.model_dump_json())
    assert reloaded == expanded
    actual = merge_construction_graphs(inputs, merge_policy=reloaded)
    stats = actual['fusion_diagnostics']['model_verified_projections']
    assert stats['applied_groups'] == stats['collapsed_nodes'] == 1
    joined, = [node for node in actual['nodes'] if len(node['supports']) == 2]
    for support in joined['supports']:
        assert len(support['corroborating_overlap_pattern_ids']) == 32


def test_a_large_reviewed_pattern_keeps_every_individually_verified_key():
    inputs = _batch()
    # One ordinary wide-table axis per branch, with a common neighbor and an
    # independent observed name for each complete typed identifier.
    for branch in inputs:
        graph = branch['graph']
        master, peer = graph['nodes']
        edge, = graph['edges']
        graph['nodes'], graph['edges'] = [peer], []
        for i in range(5001):
            node = deepcopy(master)
            key, = node['identity']
            code = f'reg-{i}'
            node.update(id=f"{master['id']}_{i}", identity={key: code}, label=f'Area {i}')
            node['supports'][0]['label'] = node['label']
            graph['nodes'].append(node)
            graph['edges'].append({**edge, 'id': f"{edge['id']}_{i}", 'source': node['id']})
    policy, report = _compile(inputs)
    assert report['verified_groups'] == 5001
    restored = MergePolicyConfig.model_validate_json(policy.model_dump_json())
    assert len(restored.verified_projection_rules[0].observation_digests) == 5001
