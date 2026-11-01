"""Exercise the actual incremental tool entry, not a final batch shortcut."""
from lightgraph.discovery.proposal_session import ProposalSession, proposal_tool_specs
from lightgraph.tests.test_object_model_supersession import fixture
from lightgraph.tests.test_schema_knowledge import packet


def test_retained_mapping_tool_copies_positions_and_keeps_aliases():
    table, knowledge, model = fixture()
    body = packet(table)
    body['scope'] = {'source_id':table.source_id,'schema':table.schema_name,'table':table.name}
    body['schema_knowledge'] = knowledge
    session = ProposalSession(body)
    assert 'submit_field_model_hierarchy' in session.names
    assert session.execute('declare_object_model', model)['status'] == 'ok'
    result = session.execute('submit_field_model_hierarchy', {
        'object_id': model['object_id'], 'plan_id': 'retained_path',
        'entity_type': 'ReportItem', 'identity_scope': 'source',
        'position_meaning': 'relative_ordinal', 'allow_multiple_parents': True,
        'repeat_is_fill': True, 'relation_type': 'parent_of',
        'evidence': [body['metadata_digest']['columns'][0]['id']],
    })
    assert result['status'] == 'ok'
    claim = session.batch().proposals[0].claim
    assert [level.key for level in claim.levels] == [p['code'] for p in model['positions']]
    assert [level.aliases for level in claim.levels] == [p['aliases'] for p in model['positions']]
    assert claim.namespace_columns == model['namespace_columns']
    assert claim.allow_dag is True  # Explicit plan choice, not a mapping default.


def test_unknown_retained_object_is_not_a_rule():
    table, knowledge, _ = fixture()
    body = packet(table); body['schema_knowledge'] = knowledge
    session = ProposalSession(body)
    result = session.execute('submit_field_model_hierarchy', {
        'object_id': 'not_issued', 'plan_id': 'path', 'entity_type': 'ReportItem',
        'identity_scope': 'source', 'position_meaning': 'relative_ordinal',
        'allow_multiple_parents': False, 'repeat_is_fill': True,
        'relation_type': 'parent_of', 'evidence': [body['metadata_digest']['columns'][0]['id']],
    })
    assert result['code'] == 'field_model_reference_unavailable'
    assert not session.batch().proposals


def test_parent_adjacency_tool_available_on_three_column_table():
    body = {'task': 'T0_discovery', 'metadata_digest': {'columns': [
        {'id': 'code', 'name': 'code'}, {'id': 'name', 'name': 'name'},
        {'id': 'parent', 'name': 'parent_code'}]}}
    names = {tool['function']['name'] for tool in proposal_tool_specs('T0_discovery', body)}
    assert 'submit_long_hierarchy' in names
    assert 'submit_long_hierarchy' in ProposalSession(body).names
