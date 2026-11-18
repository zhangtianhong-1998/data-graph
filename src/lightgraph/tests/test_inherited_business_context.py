"""Inherited names/properties may justify endpoints only in their own context."""
from copy import deepcopy
import pytest

from lightgraph.construction.contexts import business_context_matches
from lightgraph.construction.executor import execute_draft
from lightgraph.sources import MemorySource
from lightgraph.tests.test_construction_endpoint_lookup import example


def support(context, *, label='shared', ref='base:e1'):
    return {'scope_ref': 'public', 'label': label, 'properties': {'alias_code': [label]},
            'business_context': {'status': 'global' if context == {} else 'scoped', 'dimensions': context},
            'row_context': {'id': 'row_' + ref, 'values': {'observed_name': label}},
            'evidence_refs': [ref], 'observation_id': 'obs_' + ref,
            'provenance': {'source': {'source_id': 'base_source', 'schema': 'public', 'table': 'customers'}}}


def inherited_case(supports, *, main_context=None, project='K', match='shared', on_missing='error', other_nodes=(), match_column='label'):
    table, _, draft = example()
    draft['entities'] = draft['entities'][:1]
    draft.update(extends_base_graph=True, inherited_entity_types=['Customer'])
    mapping = draft['relations'][0]['mappings'][0]
    mapping['target_lookup'].update(source_id='__base_graph__', table='Customer',
        match_columns=[match_column], key_columns=['id'], on_missing=on_missing)
    if main_context is not None:
        mapping['business_context'] = main_context
        draft['entities'][0]['mappings'][0]['business_context'] = main_context
    base = {'definitions': {'entities': [{'type': 'Customer', 'key_names': ['id']}]},
            'nodes': [{'id': 'customer', 'type': 'Customer', 'identity': {'id': 'C1'},
                       'supports': deepcopy(supports)}, *deepcopy(other_nodes)]}
    with MemorySource([table], {table.qualified_name: [('O1', match, project)]}) as source:
        return execute_draft(draft, [source], [table], base_graph=base)


@pytest.mark.parametrize('match_column', ['label', 'alias_code'])
def test_k_relation_cannot_borrow_an_m_only_inherited_label(match_column):
    result = inherited_case([support({'project': 'M'}, label='M-only')], match='M-only',
        main_context={'status': 'scoped', 'columns': {'project': 'scope'}}, match_column=match_column)
    assert not result['passed'] and result['graph']['edges'] == []
    assert {'code': 'inherited_lookup_context_unmatched', 'retryable': False,
            'relation_type': 'customer', 'mapping': 0, 'source_id': 'orders'} in result['diagnostics']
    # The mapping's explicit on_missing contract still controls omission.
    omitted = inherited_case([support({'project': 'M'})], on_missing='omit',
        main_context={'status': 'scoped', 'columns': {'project': 'scope'}})
    assert omitted['passed'] and omitted['graph']['edges'] == []
    assert omitted['metrics']['lookup_inputs_omitted'] == 1


def test_global_relation_is_narrowed_to_the_actual_inherited_witness_context():
    result = inherited_case([support({'project': 'M'})], main_context={'status': 'global'})
    assert result['passed'], result['diagnostics']
    edge, = result['graph']['edges']
    observation, = edge['supports']
    assert observation['declared_business_context']['status'] == 'global'
    assert edge['business_context']['status'] == 'scoped'
    assert edge['business_context']['dimensions'] == {'project': 'M'}
    assert business_context_matches(observation, {'project': 'M'})
    assert not business_context_matches(observation, {})
    assert not business_context_matches(observation, {'project': 'K'})
    witness, = observation['lookup_matches']
    assert witness['business_context']['dimensions'] == {'project': 'M'}
    assert witness['row_context']['id'] == 'row_base:e1'
    assert witness['evidence_refs'] == observation['evidence_refs'] == ['base:e1']
    assert witness['observation_id'] == 'obs_base:e1'


def test_unknown_main_observation_never_becomes_global_through_inheritance():
    result = inherited_case([support({})])
    assert result['passed'], result['diagnostics']
    edge, = result['graph']['edges']
    assert edge['business_context']['status'] == 'unknown'
    assert not business_context_matches(edge['supports'][0], {})
    assert business_context_matches(edge['supports'][0], None)


def test_same_node_same_permission_keeps_each_context_witness_distinct():
    witnesses = [support({'project': project}, ref='base:' + project) for project in ('M', 'K')]
    global_main = inherited_case(witnesses, main_context={'status': 'global'})
    assert global_main['passed'], global_main['diagnostics']
    assert len(global_main['graph']['edges']) == 2
    assert {edge['business_context']['dimensions']['project'] for edge in global_main['graph']['edges']} == {'M', 'K'}
    scoped_main = inherited_case(witnesses, main_context={'status': 'scoped', 'columns': {'project': 'scope'}})
    assert scoped_main['passed'], scoped_main['diagnostics']
    edge, = scoped_main['graph']['edges']
    assert edge['business_context']['dimensions'] == {'project': 'K'}
    assert edge['supports'][0]['lookup_matches'][0]['evidence_refs'] == ['base:K']


def test_context_filtering_precedes_inherited_identity_cardinality_check():
    other = {'id': 'customer_k', 'type': 'Customer', 'identity': {'id': 'C2'},
             'supports': [support({'project': 'K'}, ref='base:K')]}
    result = inherited_case([support({'project': 'M'})], other_nodes=[other],
        main_context={'status': 'scoped', 'columns': {'project': 'scope'}})
    assert result['passed'], result['diagnostics']
    assert result['graph']['edges'][0]['target'] == 'customer_k'


def test_two_inherited_endpoints_cannot_cross_disjoint_witness_contexts():
    table, _, draft = example()
    draft.update(entities=[], extends_base_graph=True, inherited_entity_types=['Order', 'Customer'])
    mapping = draft['relations'][0]['mappings'][0]
    mapping['business_context'] = {'status': 'global'}
    mapping['target_lookup'].update(source_id='__base_graph__', table='Customer', match_columns=['label'], key_columns=['id'])
    base = {'definitions': {'entities': [{'type': kind, 'key_names': ['id']} for kind in ('Order', 'Customer')]},
            'nodes': [{'id': 'order', 'type': 'Order', 'identity': {'id': 'O1'}, 'supports': [support({'project': 'M'})]},
                      {'id': 'customer', 'type': 'Customer', 'identity': {'id': 'C1'}, 'supports': [support({'project': 'K'})]}]}
    with MemorySource([table], {table.qualified_name: [('O1', 'shared', 'unused')]}) as source:
        result = execute_draft(draft, [source], [table], base_graph=base)
    assert not result['passed'] and not result['graph']['edges']
    assert any(item['code'] == 'inherited_lookup_context_conflict' for item in result['diagnostics'])
