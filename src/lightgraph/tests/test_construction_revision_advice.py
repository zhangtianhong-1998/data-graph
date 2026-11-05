"""Revision losses remain visible without turning legitimate edits into errors."""
from copy import deepcopy

from lightgraph.construction.models import GraphDraft
from lightgraph.construction.revision_advice import revision_loss_advice


def example():
    return {'draft_id': 'graph', 'entities': [
        {'type': 'Item', 'key_names': ['id'], 'mappings': [
            {'source_id': 'file', 'table': 'paths', 'key_columns': ['a'], 'label_column': 'an',
             'context_columns': ['a', 'b'], 'predicate': {'column': 'a', 'op': 'eq', 'value': 'SECRET_PREDICATE'},
             'constant_properties': {'note': 'SECRET_CONSTANT'}},
            {'source_id': 'file', 'table': 'paths', 'key_columns': ['b'], 'label_column': 'bn',
             'context_columns': ['a', 'b']}]},
        {'type': 'Customer', 'key_names': ['id'], 'mappings': [
            {'source_id': 'db', 'table': 'customers', 'key_columns': ['id'], 'label_column': 'title'}]}],
        'relations': [{'type': 'contains', 'source_type': 'Item', 'target_type': 'Item', 'mappings': [
            {'source_id': 'file', 'table': 'paths', 'source_key_columns': ['a'], 'target_key_columns': ['b'],
             'context_columns': ['a', 'b']}]},
            {'type': 'belongs_to', 'source_type': 'Item', 'target_type': 'Customer', 'mappings': [
                {'source_id': 'file', 'table': 'orders', 'source_key_columns': ['item'],
                 'target_key_columns': ['email'], 'target_lookup': {
                     'source_id': 'lookup', 'table': 'contacts', 'match_columns': ['email'], 'key_columns': ['id']}}]}]}


def test_no_previous_revision_or_no_loss_returns_no_advice():
    value = example()
    assert revision_loss_advice(None, value) == []
    assert revision_loss_advice(value, GraphDraft.model_validate(value)) == []
    reordered = deepcopy(value)
    reordered['entities'].reverse()
    reordered['entities'][1]['mappings'].reverse()
    reordered['relations'].reverse()
    assert revision_loss_advice(value, reordered) == []


def test_removing_database_and_relation_keeps_exact_declaration_loss_visible():
    previous = example()
    current = deepcopy(previous)
    current['entities'] = current['entities'][:1]
    current['relations'] = current['relations'][:1]
    untouched = deepcopy((previous, current))
    advice = revision_loss_advice(previous, current)
    by_code = {item['code']: item for item in advice}
    assert by_code['revision_sources_removed']['source_ids'] == ['db', 'lookup']
    assert by_code['revision_sources_removed']['tables'] == [
        {'source_id': 'db', 'schema': 'public', 'table': 'customers'},
        {'source_id': 'file', 'schema': 'public', 'table': 'orders'},
        {'source_id': 'lookup', 'schema': 'public', 'table': 'contacts'}]
    assert by_code['revision_entities_removed']['removed_types'] == ['Customer']
    assert by_code['revision_relations_removed']['removed_types'] == ['belongs_to']
    assert all(item['severity'] == 'suggestion' for item in advice)
    assert all('修订历史' in item['next_action'] and '合法的范围调整' in item['next_action'] for item in advice)
    assert (previous, current) == untouched
    assert 'SECRET_' not in str(advice)
    GraphDraft.model_validate(current)  # Feedback does not invalidate an intentional scope edit.


def test_same_physical_mapping_reports_primary_name_demotion_and_context_loss():
    previous = example()
    current = deepcopy(previous)
    mapping = current['entities'][0]['mappings'][0]
    mapping.pop('label_column')
    mapping['alias_columns'] = ['an']
    mapping['context_columns'] = ['a']
    advice = revision_loss_advice(previous, current)
    by_code = {item['code']: item for item in advice}
    assert set(by_code) == {'revision_primary_name_role_changed', 'revision_context_columns_removed'}
    name = by_code['revision_primary_name_role_changed']
    assert name['column'] == 'an' and name['current_roles'] == ['alias']
    assert name['previous_path'] == ['entities', 0, 'mappings', 0]
    assert name['matching_paths'] == [['entities', 0, 'mappings', 0]]
    assert name['source'] == {'source_id': 'file', 'schema': 'public', 'table': 'paths'}
    assert by_code['revision_context_columns_removed']['removed_columns'] == ['b']
    assert 'SECRET_' not in str(advice)


def test_mapping_identity_includes_schema_and_ordered_endpoint_keys():
    previous = example()
    current = deepcopy(previous)
    current['entities'][0]['mappings'][0]['schema'] = 'other'
    current['relations'][0]['mappings'][0].update(source_key_columns=['b'], target_key_columns=['a'])
    removed = [item for item in revision_loss_advice(previous, current) if item['code'] == 'revision_mappings_removed']
    assert len(removed) == 2
    assert removed[0]['key_columns'] == ['a']
    assert removed[1]['source_key_columns'] == ['a'] and removed[1]['target_key_columns'] == ['b']


def test_compatible_mapping_can_preserve_name_and_context_at_another_index():
    previous = example()
    current = deepcopy(previous)
    original = deepcopy(current['entities'][0]['mappings'][0])
    current['entities'][0]['mappings'][0].update(label_column=None, alias_columns=['an'], context_columns=[])
    current['entities'][0]['mappings'].append(original)
    assert revision_loss_advice(previous, current) == []


def test_removed_relation_context_is_reported_without_deleting_its_mapping():
    previous = example()
    current = deepcopy(previous)
    current['relations'][0]['mappings'][0]['context_columns'] = []
    advice = revision_loss_advice(previous, current)
    assert len(advice) == 1 and advice[0]['code'] == 'revision_context_columns_removed'
    assert advice[0]['definition_kind'] == 'relation' and advice[0]['definition_type'] == 'contains'
    assert advice[0]['previous_path'] == ['relations', 0, 'mappings', 0]
    assert advice[0]['removed_columns'] == ['a', 'b']
    GraphDraft.model_validate(current)


def test_name_replaced_or_removed_does_not_imply_alias_equivalence():
    previous = example()
    current = deepcopy(previous)
    current['entities'][0]['mappings'][0].update(label_column='alternative', properties={'description': 'an'})
    advice = revision_loss_advice(previous, current)
    assert len(advice) == 1
    assert advice[0]['current_roles'] == ['property']
    assert advice[0]['current_label_columns'] == ['alternative']
    current['entities'][0]['mappings'][0].update(label_column=None, properties={})
    advice = revision_loss_advice(previous, current)
    assert advice[0]['current_roles'] == ['unmapped']
