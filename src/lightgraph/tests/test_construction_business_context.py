"""Identity grain, qualified facts and provenance must stay independent."""
from copy import deepcopy

import pytest

from lightgraph.contracts import ColumnSpec, TableSpec, ExtractionGoal
from lightgraph.construction.contexts import (business_context_matches, normalize_context_selection,
                                             support_business_context)
from lightgraph.construction.executor import execute_draft
from lightgraph.construction.models import GraphDraft
from lightgraph.construction.node_standardizer import standardize_node_details
from lightgraph.errors import LightGraphError
from lightgraph.sources import MemorySource


def fixture(*, scoped_identity=False, context=True):
    table = TableSpec(source_id='C', name='items', columns=[ColumnSpec(name=name) for name in
        ['project', 'code', 'parent', 'amount', 'period']])
    mapping = {'source_id': 'C', 'table': 'items', 'key_columns': ['code'],
               'properties': {'amount': 'amount'}, 'context_columns': ['project', 'period']}
    if context:
        mapping['business_context'] = {'status': 'scoped', 'columns': {'project': 'project', 'period': 'period'}}
    entity = {'type': 'Item', 'key_names': ['code'], 'mappings': [mapping]}
    if scoped_identity:
        entity.update(identity_scope='scoped', identity_context_keys=['project'], key_names=['project', 'code'])
        mapping['key_columns'] = ['project', 'code']
    return table, {'draft_id': 'contexts', 'entities': [entity]}


def execute(table, draft, rows, **kwargs):
    with MemorySource([table], {table.qualified_name: rows}) as source:
        return execute_draft(draft, [source], [table], label_conflict_policy='retain_variants', **kwargs)


def test_project_identity_is_explicit_and_relations_do_not_cross_projects():
    table, draft = fixture(scoped_identity=True, context=False)
    parent = deepcopy(draft['entities'][0]['mappings'][0])
    parent['key_columns'] = ['project', 'parent']
    parent['properties'] = {}
    draft['entities'][0]['mappings'].append(parent)
    draft['relations'] = [{'type': 'parent', 'source_type': 'Item', 'target_type': 'Item', 'mappings': [{
        'source_id': 'C', 'table': 'items', 'source_key_columns': ['project', 'code'],
        'target_key_columns': ['project', 'parent'],
        'business_context': {'status': 'scoped', 'columns': {'project': 'project'}}}]}]
    result = execute(table, draft, [('M', '001', 'ROOT', 100, 'Q1'), ('K', '001', 'ROOT', 200, 'Q1')])
    assert result['passed'], result['diagnostics']
    nodes = {node['id']: node for node in result['graph']['nodes']}
    assert len(nodes) == 4 and len(result['graph']['edges']) == 2
    for edge in result['graph']['edges']:
        assert nodes[edge['source']]['identity']['project'] == nodes[edge['target']]['identity']['project']
    for node in nodes.values():
        context = support_business_context(node['supports'][0])
        assert context['status'] == 'scoped' and context['dimensions'] == {'project': node['identity']['project']}


def test_global_object_keeps_project_and_time_qualified_values_as_observations():
    table, draft = fixture()
    draft['entities'][0].update(identity_scope='global', identity_domain='catalog', identity_evidence_refs=['e1'])
    result = execute(table, draft, [('M', '001', 'ROOT', 100, 'Q1'), ('K', '001', 'ROOT', 200, 'Q2')])
    assert result['passed'], result['diagnostics']
    node, = result['graph']['nodes']
    assert node['identity_context']['status'] == 'global'
    assert all(support['evidence_refs'] == ['e1'] for support in node['supports'])
    details = standardize_node_details(node)
    observations = details['attributes'][0]['observations']
    assert {(item['value'], item['business_context']['dimensions']['project'],
             item['business_context']['dimensions']['period']) for item in observations} == {
                 (100, 'M', 'Q1'), (200, 'K', 'Q2')}
    assert len(node['supports']) == 2


def test_same_endpoints_in_two_contexts_keep_two_qualified_relations():
    table, draft = fixture()
    parent = deepcopy(draft['entities'][0]['mappings'][0])
    parent['key_columns'] = ['parent']
    parent['properties'] = {}
    draft['entities'][0]['mappings'].append(parent)
    draft['relations'] = [{'type': 'parent', 'source_type': 'Item', 'target_type': 'Item', 'mappings': [{
        'source_id': 'C', 'table': 'items', 'source_key_columns': ['code'], 'target_key_columns': ['parent'],
        'properties': {'amount': 'amount'},
        'business_context': {'status': 'scoped', 'columns': {'project': 'project', 'period': 'period'}}}]}]
    rows = [('M', '001', 'ROOT', 100, 'Q1'), ('K', '001', 'ROOT', 200, 'Q2')]
    result = execute(table, draft, rows)
    assert result['passed'], result['diagnostics']
    assert len(result['graph']['nodes']) == 2
    edges = result['graph']['edges']
    assert len(edges) == 2 and len({edge['source'] for edge in edges}) == 1
    assert {(edge['business_context']['dimensions']['project'], edge['properties']['amount'][0])
            for edge in edges} == {('M', 100), ('K', 200)}
    replay = execute(table, draft, list(reversed(rows)))
    assert {edge['id'] for edge in replay['graph']['edges']} == {edge['id'] for edge in edges}


def test_legacy_and_row_provenance_do_not_imply_global_business_applicability():
    table, draft = fixture(context=False)
    result = execute(table, draft, [('M', '001', 'ROOT', 100, 'Q1')])
    node, = result['graph']['nodes']
    support, = node['supports']
    assert support['row_context']['values'] == {'project': 'M', 'period': 'Q1'}
    assert support_business_context(support)['status'] == 'unknown'
    legacy = {'scope_ref': 'M', 'row_context': {'values': {'project': 'M'}}}
    assert business_context_matches(legacy, None)
    assert not business_context_matches(legacy, {})
    assert not business_context_matches(legacy, {'project': 'M'})
    assert standardize_node_details({'id': 'old', 'supports': [legacy]})['business_contexts'][0]['status'] == 'unknown'


def test_null_context_retains_the_observation_without_claiming_global_scope():
    table, draft = fixture()
    result = execute(table, draft, [(None, '001', 'ROOT', 100, 'Q1')])
    assert result['passed'], result['diagnostics']
    support = result['graph']['nodes'][0]['supports'][0]
    assert support['properties']['amount'] == [100]
    assert support_business_context(support)['status'] == 'unknown'
    assert support['business_context']['missing_dimensions'] == ['project']
    assert business_context_matches(support, None)
    assert not business_context_matches(support, {'period': 'Q1'})


def test_contradictory_context_keeps_the_fact_as_unknown_without_aborting_other_rows():
    table, draft = fixture(scoped_identity=True)
    draft['entities'][0]['mappings'][0]['business_context'] = {
        'status': 'scoped', 'values': {'project': 'M'}}
    result = execute(table, draft, [('M', '001', 'ROOT', 100, 'Q1'), ('K', '001', 'ROOT', 200, 'Q2')])
    assert result['passed'], result['diagnostics']
    nodes = {node['identity']['project']: node for node in result['graph']['nodes']}
    assert len(nodes) == 2
    accepted = nodes['M']['supports'][0]
    ambiguous = nodes['K']['supports'][0]
    assert business_context_matches(accepted, {'project': 'M'})
    assert ambiguous['properties'] == {'amount': [200]}
    assert ambiguous['business_context']['status'] == 'unknown'
    assert ambiguous['business_context']['conflicts'][0]['reason'] == 'business_context_identity_conflict'
    assert not business_context_matches(ambiguous, {'project': 'M'})
    assert not business_context_matches(ambiguous, {'project': 'K'})
    assert business_context_matches(ambiguous, None)


def test_business_context_is_conjunctive_and_distinct_from_access_scope():
    global_support = {'scope_ref': 'private', 'business_context': {'status': 'global', 'dimensions': {}}}
    scoped = {'scope_ref': 'public', 'business_context': {
        'status': 'scoped', 'dimensions': {'project': 'M', 'period': 'Q1'}}}
    assert business_context_matches(global_support, {})
    assert business_context_matches(global_support, {'project': 'K'})
    assert business_context_matches(scoped, {'project': 'M'})
    assert not business_context_matches(scoped, {})
    assert not business_context_matches(scoped, {'project': 'M', 'period': 'Q2'})
    assert not business_context_matches(scoped, {'owning_project': 'M'})
    assert not business_context_matches({'business_context': {'status': 'scoped', 'dimensions': {'project': True}}}, {'project': 1})
    assert normalize_context_selection({}) == {}
    for invalid in ([], {'project': []}, {'project': None}, {'project': float('nan')}):
        with pytest.raises(LightGraphError, match='invalid_business_context_selection'):
            normalize_context_selection(invalid)


def test_context_projection_respects_column_authorization():
    table, draft = fixture()
    result = execute(table, draft, [('M', '001', 'ROOT', 100, 'Q1')], goal=ExtractionGoal(excluded_columns=['project']))
    assert not result['passed'] and not result['graph']['nodes']


def test_identity_declaration_must_include_scope_in_actual_keys_and_global_evidence():
    _, draft = fixture()
    draft['entities'][0].update(identity_scope='scoped', identity_context_keys=['project'])
    with pytest.raises(ValueError, match='identity_context_key_not_in_identity'):
        GraphDraft.model_validate(draft)
    draft['entities'][0].update(identity_scope='global', identity_context_keys=[])
    with pytest.raises(ValueError, match='global_identity_requires_domain_evidence'):
        GraphDraft.model_validate(draft)
    draft['entities'][0].update(identity_domain='catalog', identity_evidence_refs=['e1'],
        key_names=['source_id', 'schema', 'table', 'column'], mappings=[{
            'kind': 'metadata_column', 'source_id': 'C', 'table': 'items', 'column': 'code', 'evidence_refs': ['e1']}])
    with pytest.raises(ValueError, match='metadata_column_business_identity_forbidden'):
        GraphDraft.model_validate(draft)
