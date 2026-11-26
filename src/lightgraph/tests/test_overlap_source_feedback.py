"""A wrong table selection can be revised against frozen observations in the same review."""
import json
import time

import pytest

from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.construction.overlap_feedback import reconsider_source_selections
from lightgraph.construction.overlap_patterns import analyze_overlap_patterns
from lightgraph.construction.projection_proposals import verify_projection_proposals
from lightgraph.runtime.config import MergePolicyConfig, WorkflowMergeNodeConfig
from lightgraph.tests.test_batch_overlap_patterns import unit
from lightgraph.tests.test_overlap_pattern_model import configured, respond, setup_provider


def _two_tables():
    a = unit('a', 'Area', 'code')
    b = unit('b', 'Zone', 'code', field='length')
    archive = unit('historical', 'Zone', 'code', table='archive', field='length')
    for node in archive['graph']['nodes']:
        node['identity']['code'] += 'X'
    for field in ('nodes', 'edges'):
        b['graph'][field].extend(archive['graph'][field])
    return [a, b]


def _select(card, tables):
    selection = []
    for i, (member, table) in enumerate(zip(card['members'], tables, strict=True)):
        source = next(j for j in member['source_indexes'] if card['sources'][j]['table'] == table)
        role = next(j for j, r in enumerate(member['roles']) if r['end'] == 'source' and source in r['source_indexes'])
        selection.append({'member_index': i, 'source_index': source, 'role_index': role})
    return {'pattern_id': card['pattern_id'], 'action': 'propose', 'alignment': selection}


@pytest.mark.parametrize('outcome', ['corrected', 'withdrawn', 'budget_exhausted'])
def test_reconsider_a_selected_empty_table_without_touching_a_hard_identity_boundary(monkeypatch, outcome):
    inputs = _two_tables()
    card = next(p for p in analyze_overlap_patterns(inputs).patterns if p['basis'] == 'same_logical_keys_across_sources')
    prior = _select(card, ['items', 'archive'])
    correction = _select(card, ['items', 'items'])
    policy, checks = verify_projection_proposals(MergePolicyConfig(), inputs, [card], [prior])
    assert checks[0]['reason'] == 'no_shared_keys'
    result = {'decisions': [prior], 'model_calls': 1}
    def reply(request):
        payload = json.loads(request.content)
        issued, = json.loads(payload['messages'][1]['content'])['patterns']
        assert issued['previous_check'] == {'alignment': prior['alignment'], 'reason': 'no_shared_keys'}
        assert '000071' not in str(payload) and 'private_value' not in str(payload)
        decision = correction if outcome == 'corrected' else {'action': 'review', 'pattern_id': card['pattern_id']}
        return respond(json.dumps({'decisions': [decision]}))
    calls = setup_provider(monkeypatch, reply)
    limit = 1 if outcome == 'budget_exhausted' else 2
    effective, final = reconsider_source_selections(
        configured(), WorkflowMergeNodeConfig(node_id='merge', max_calls=limit), inputs, [card], result, policy, checks,
        model_api_key='test', check_cancelled=lambda: None, on_model_call=None, deadline=time.monotonic()+30)
    assert result['model_calls'] == len(calls) + 1 == limit
    assert result['source_selection_review']['prior'][card['pattern_id']]['check'] == checks[0]
    if outcome == 'corrected':
        assert final[0]['verified_groups'] == 1
        base = merge_construction_graphs(inputs)
        graph = merge_construction_graphs(inputs, merge_policy=effective)
        assert len(graph['nodes']) == len(base['nodes']) - 1
        assert len(graph['edges']) == len(base['edges']) == 3
        assert graph == merge_construction_graphs(inputs, merge_policy=MergePolicyConfig.model_validate_json(
            effective.model_dump_json()))
    else:
        assert effective == policy and final == ([] if outcome == 'withdrawn' else checks)


def test_cross_table_identity_refusal_does_not_offer_speculative_alternatives():
    inputs = [unit('a', 'Alpha', 'code', table='actuals'), unit('b', 'Beta', 'code', table='targets')]
    card = next(p for p in analyze_overlap_patterns(inputs).patterns if p['basis'] == 'same_logical_keys_across_sources')
    decision = _select(card, ['actuals', 'targets'])
    policy, checks = verify_projection_proposals(MergePolicyConfig(), inputs, [card], [decision])
    assert checks[0]['reason'] == 'cross_table_identity_unverified'
    result = {'decisions': [decision], 'model_calls': 1}
    assert reconsider_source_selections(
        configured(), WorkflowMergeNodeConfig(node_id='merge'), inputs, [card], result, policy, checks,
        model_api_key=None, check_cancelled=lambda: None, on_model_call=None, deadline=time.monotonic()+1) == (policy, checks)
    assert result == {'decisions': [decision], 'model_calls': 1}


def test_a_one_call_budget_prioritizes_observed_key_and_name_overlap_over_the_opaque_id(monkeypatch):
    from copy import deepcopy
    from lightgraph.construction.overlap_review import review_overlap_patterns
    inputs = _two_tables()
    template = next(p for p in analyze_overlap_patterns(inputs).patterns if p['basis'] == 'same_logical_keys_across_sources')
    cards = []
    for i in range(9):
        p = deepcopy(template)
        p['pattern_id'] = f'pattern_{i:024x}'
        if i != 8:
            p.pop('binding_comparisons')
        cards.append(p)
    def reply(request):
        issued = json.loads(json.loads(request.content)['messages'][1]['content'])['patterns']
        return respond(json.dumps({'decisions': [{'pattern_id': p['pattern_id'], 'action': 'review'} for p in issued]}))
    calls = setup_provider(monkeypatch, reply)
    result = review_overlap_patterns(configured(), cards, max_calls=1, api_key='test')
    assert len(calls) == 1 and result['batches'][0]['pattern_ids'][0] == cards[-1]['pattern_id']
    assert result['reviewed_patterns'] + result['omitted_patterns'] == len(cards)
