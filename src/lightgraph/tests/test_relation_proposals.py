from copy import deepcopy
import json
import time

import pytest

from lightgraph.construction.overlap_review import _decisions, _structural_profile
from lightgraph.construction.relation_proposals import annotate_relation_meanings, verify_relation_proposals
from lightgraph.tests.test_fusion_census_edges import _verified_edge


def _case():
    edges = [_verified_edge('edge-a', 'business_members', [[2, 2], [3, 4]]),
             _verified_edge('edge-b', 'posted_children', [[2, 4]])]
    definitions = []
    inputs, members = [], []
    for owner, edge in zip(('branch-a', 'branch-b'), edges):
        definition = {'type': edge['type'], 'source_type': 'Account', 'target_type': 'Account',
                      'description': 'Direct posting-account parent in the frozen ledger tree.',
                      'hierarchy_orientation': 'parent_to_child'}
        edge['supports'][0]['contributor_id'] = owner
        definitions.append(definition)
        inputs.append({'contributor_id': owner, 'graph': {'definitions': {'relations': [deepcopy(definition)]}}})
        members.append({'contributor_id': owner, 'type': edge['type'], 'key_names': []})
    graph = {'edges': edges, 'nodes': [{'id': n, 'type': 'Account'} for n in ('p', 'c', 'p2')],
             'definitions': {'relations': definitions}}
    prov = edges[0]['supports'][0]['provenance']
    card = {'pattern_id': 'pattern_' + 'f' * 24, 'kind': 'relation', 'basis': 'same_directed_source_columns',
            'overlapping_keys': 2, 'eligible_actions': ['retain'], 'reasons': ['preserve_relation_observations'],
            'members': members, 'sources': [{**prov['source'], 'source_key_columns': prov['source_key_columns'],
                                             'target_key_columns': prov['target_key_columns']}]}
    decision = {'pattern_id': card['pattern_id'], 'action': 'propose_relation', 'member_indexes': [0, 1]}
    return graph, inputs, card, decision


def test_model_proposes_only_issued_relation_members_and_row_verification_keeps_original_edges():
    graph, inputs, card, decision = _case()
    original = deepcopy(graph)
    annotate_relation_meanings([card], inputs)
    profile = _structural_profile(card)
    assert [m['meaning_group'] for m in profile['members']] == [0, 0]
    assert 'frozen ledger' not in str(profile)
    pid = card['pattern_id']
    reply = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(
        {'decisions': [{**decision, 'member_indexes': [1, 0]}]})}}]}
    decisions = _decisions(reply, {pid: profile})
    assert decisions == [decision]
    report, = verify_relation_proposals(graph, [card], decisions)
    assert (report['examined_edges'], report['candidate_endpoint_groups'], report['confirmed_fact_groups'],
            report['grouped_output_edges']) == (2, 1, 1, 2)
    assert report['groups'][0]['edge_ids'] == ['edge-a', 'edge-b'] and report['rejected_by_reason'] == {}
    assert 'frozen ledger' not in str(report) and 'AREA_L' not in str(report) and graph == original
    graph['edges'].reverse()
    assert report == verify_relation_proposals(graph, [card], decisions)[0]


@pytest.mark.parametrize('conflict', [None, 'description', 'family', 'constraint', 'missing_declaration',
                                      'different_row', 'unknown_scope', 'different_direction'])
def test_ordinary_relation_facts_need_the_same_declared_meaning_and_qualified_rows(conflict):
    graph, inputs, card, decision = _case()
    for i, (unit, definition, edge, member) in enumerate(zip(
            inputs, graph['definitions']['relations'], graph['edges'], card['members'], strict=True)):
        family = 'serves' if conflict == 'family' and i else 'located_in'
        original = dict(type=family, source_type='Region', target_type='Country',
                        description='Region belongs to this reporting country.', acyclic=False)
        if i and conflict == 'description':
            original['description'] = 'Country of tax residence.'
        if i and conflict == 'constraint':
            original['acyclic'] = True
        if conflict == 'missing_declaration':
            original.pop('description')
        unit['graph']['definitions']['relations'] = [original]
        member['type'] = family
        definition.clear()
        definition.update(original, type=edge['type'], original_type=family,
                          source_type='Account', target_type='Account')
        support = edge['supports'][0]
        support['provenance']['definition'] = family
        support['endpoint_origins'] = [{'input_kind': 'unit', 'contributor_id': unit['contributor_id'], 'type': family}]
    support = graph['edges'][1]['supports'][0]
    if conflict == 'different_row':
        support['source_row_witness']['rows'] = [[9, 9]]
    if conflict == 'unknown_scope':
        support.pop('business_context')
    if conflict == 'different_direction':
        edge = graph['edges'][1]
        edge['source'], edge['target'] = edge['target'], edge['source']
    original_graph = deepcopy(graph)
    annotate_relation_meanings([card], inputs)
    result, = verify_relation_proposals(graph, [card], [decision])
    assert graph == original_graph
    if conflict:
        assert result['groups'] == [] and result['grouped_output_edges'] == 0
    else:
        fact, = result['groups']
        assert fact['fact_kind'] == 'direct_relationship'
        assert fact['edge_ids'] == ['edge-a', 'edge-b']
        legacy = deepcopy(card)
        del legacy['relation_verification_version']
        for member in legacy['members']:
            member['meaning_group'] = None
            member.pop('declared_meaning_digest')
        old, = verify_relation_proposals(graph, [legacy], [decision])
        assert old['groups'] == []
        assert old['rejected_by_reason'] == {'no_declared_hierarchy_semantics': 2}


@pytest.mark.parametrize('conflict', ['different_declared_usage', 'original_conflicting_usage', 'missing_orientation',
                                     'reversed', 'level', 'snapshot', 'partial_rows', 'missing_row',
                                     'permission', 'unknown_scope', 'different_value', 'unaligned_endpoint',
                                     'unknown_contributor', 'scope_fingerprint', 'self_loop'])
def test_proposed_relations_do_not_share_a_fact_with_incomplete_or_conflicting_evidence(conflict):
    graph, inputs, card, decision = _case()
    edge = graph['edges'][1]
    support = edge['supports'][0]
    definition = graph['definitions']['relations'][1]
    if conflict == 'different_declared_usage':
        definition['description'] = 'Direct management oversight in a different hierarchy.'
    elif conflict == 'original_conflicting_usage':
        inputs[1]['graph']['definitions']['relations'][0]['description'] = 'A distinct compliance hierarchy.'
    elif conflict == 'missing_orientation':
        definition.pop('hierarchy_orientation')
    elif conflict == 'reversed':
        edge['source'], edge['target'] = edge['target'], edge['source']
    elif conflict == 'level':
        support['provenance']['target_key_columns'] = ['AREA_L3_ID']
    elif conflict == 'snapshot':
        support['source_row_witness']['snapshot'] = 'b' * 64
    elif conflict == 'partial_rows':
        support['source_row_witness']['rows'] = [[2, 3]]
    elif conflict == 'missing_row':
        support.pop('source_row_witness')
    elif conflict == 'permission':
        support['required_scope_refs'] = ['finance']
    elif conflict == 'unknown_scope':
        support.pop('business_context')
    elif conflict == 'different_value':
        support['properties']['basis'] = ['planned']
    elif conflict == 'unaligned_endpoint':
        edge['source'] = 'p2'
    elif conflict == 'unknown_contributor':
        support.pop('contributor_id')
    elif conflict == 'scope_fingerprint':
        card['reasons'].append('unverified_access_fingerprint')
    elif conflict == 'self_loop':
        for record in graph['edges']:
            record['target'] = record['source']
    annotate_relation_meanings([card], inputs)
    report, = verify_relation_proposals(graph, [card], [decision])
    assert not report['groups'] and report['status'] == 'review'
    assert report['rejected_by_reason'] and sum(report['rejected_by_reason'].values()) == report['examined_edges']


@pytest.mark.parametrize('indexes,kind', [([0, 0], 'relation'), ([0, 2], 'relation'), ([-1, 1], 'relation'),
                                           ([False, 1], 'relation'), ([0, 1], 'entity')])
def test_model_cannot_select_invalid_relation_members(indexes, kind):
    _, _, card, decision = _case()
    card['kind'] = kind
    value = {'decisions': [{**decision, 'member_indexes': indexes}]}
    with pytest.raises(ValueError):
        _decisions({'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(value)}}]},
                   {card['pattern_id']: _structural_profile(card)})


def test_merge_checkpoint_archives_proposed_relation_fact_and_both_source_edges(tmp_path, monkeypatch):
    from lightgraph.construction import checkpoints
    from lightgraph.construction.workflow import save_workflow_state
    from lightgraph.construction.workflow_overlap import merge_with_overlap_review
    from lightgraph.runtime.config import FusionPolicyConfig, MergePolicyConfig, WorkflowMergeNodeConfig
    from lightgraph.tests.test_batch_overlap_patterns import unit
    from lightgraph.tests.test_overlap_pattern_model import configured, respond, setup_provider
    from lightgraph.web.execution import _workbench_workflow

    original, _, _, _ = _case()
    run_id = 'run_' + 'f' * 24
    ids = ['branch-a', 'branch-b']
    artifacts, inputs = {}, []
    for owner, edge, definition in zip(ids, original['edges'], original['definitions']['relations'], strict=True):
        item = unit(owner, 'Account', 'account_id')
        graph = item['graph']
        graph['definitions']['relations'] = [definition]
        graph['edges'] = [edge]
        for node, nid, column in zip(graph['nodes'], ('p', 'c'), ('AREA_L1_ID', 'AREA_L2_ID'), strict=True):
            node['id'] = nid
            node['supports'][0]['provenance'].update(source=edge['supports'][0]['provenance']['source'],
                                                     key_columns=[column])
        record = checkpoints.commit_checkpoint(tmp_path, run_id, owner, item,
                                              input_digest='i', graph_kind='node_delta')
        artifacts[owner] = record
        inputs.append(checkpoints.load_checkpoint(tmp_path, record))

    def reply(request):
        cards = json.loads(json.loads(request.content)['messages'][1]['content'])['patterns']
        decisions = []
        for card in cards:
            decision = {'pattern_id': card['pattern_id'], 'action': 'keep'}
            if card['kind'] == 'relation':
                assert [member['meaning_group'] for member in card['members']] == [0, 0]
                decision.update(action='propose_relation', member_indexes=[0, 1])
            decisions.append(decision)
        return respond(json.dumps({'decisions': decisions}))

    requests = setup_provider(monkeypatch, reply)
    policy = MergePolicyConfig(shared_entity_types=['Account'], fusion_policy=FusionPolicyConfig())
    node = WorkflowMergeNodeConfig(node_id='merge', merge_mode='overlap_review')
    def load_index(item):
        return checkpoints.load_checkpoint_overlap_index(tmp_path, artifacts[item['contributor_id']])

    merged, report = merge_with_overlap_review(configured(), node, policy, inputs, ids, artifacts,
        inherited=False, source_namespace='workspace1', model_api_key='test-key', check_cancelled=lambda: None,
        deadline=time.monotonic() + 30, load_overlap_index=load_index)
    summary = report['summary']
    assert len(requests) == summary['model_calls'] == 1
    assert summary['verified_relation_facts'] == 1, report['relation_proposal_checks']
    assert summary['grouped_relation_edges'] == 2
    result = checkpoints.commit_checkpoint(tmp_path, run_id, 'merge',
        {'kind': 'merge', 'graph': merged, 'overlap_review': report}, input_digest='m',
        upstream_artifacts=[artifacts[owner]['artifact_id'] for owner in ids])
    archived = checkpoints.load_checkpoint(tmp_path, result)
    check, = archived['overlap_review']['relation_proposal_checks']
    fact, = check['groups']
    edges = archived['graph']['edges']
    assert sorted(edge['id'] for edge in edges) == fact['edge_ids'] and len(fact['edge_ids']) == 2
    assert {edge['type'] for edge in edges} == {edge['type'] for edge in original['edges']}
    for edge in edges:
        support, = edge['supports']
        source = next(e for e in original['edges'] if e['type'] == edge['type'])
        original_ids = {o['edge_id'] for o in support.get('endpoint_origins', [])}
        assert source['id'] in original_ids or edge['id'] == source['id']
        assert support['source_row_witness'] == source['supports'][0]['source_row_witness']
    save_workflow_state(tmp_path, {'parent_run_id': run_id, 'merge_nodes': {'merge': {'overlap_review': summary}}})
    visible = _workbench_workflow(tmp_path, run_id)['merge_nodes']['merge']['overlap_review']
    assert visible['verified_relation_facts'] == 1 and visible['grouped_relation_edges'] == 2
    assert 'relation_proposal_checks' not in visible and fact['fact_id'] not in str(visible)
    assert not any(edge_id in str(visible) for edge_id in fact['edge_ids'])
