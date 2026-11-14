"""Actual row matching for foreign keys and self joins, including visibility."""
import pytest
from pydantic import ValidationError

from lightgraph.contracts import ColumnSpec, TableSpec, ExtractionGoal
from lightgraph.sources import MemorySource
from lightgraph.construction.access import filter_graph_scopes
from lightgraph.construction.executor import execute_draft
from lightgraph.construction.models import GraphDraft
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import dump_yaml
from lightgraph.runtime.config import RunConfig


def example():
    orders = TableSpec(source_id='orders', name='orders', columns=[ColumnSpec(name=c)
        for c in ['order_id', 'email', 'scope']])
    customers = TableSpec(source_id='crm', name='customers', columns=[ColumnSpec(name=c)
        for c in ['customer_id', 'email', 'name', 'scope']])
    draft = {'draft_id': 'lookup', 'entities': [
        {'type': 'Order', 'key_names': ['id'], 'mappings': [
            {'source_id': 'orders', 'table': 'orders', 'key_columns': ['order_id']}]},
        {'type': 'Customer', 'key_names': ['id'], 'mappings': [
            {'source_id': 'crm', 'table': 'customers', 'key_columns': ['customer_id'], 'label_column': 'name'}]}],
        'relations': [{'type': 'customer', 'source_type': 'Order', 'target_type': 'Customer', 'mappings': [
            {'source_id': 'orders', 'table': 'orders', 'source_key_columns': ['order_id'],
             'target_key_columns': ['email'], 'target_lookup': {'source_id': 'crm', 'table': 'customers',
                'match_columns': ['email'], 'key_columns': ['customer_id']}}]}]}
    return orders, customers, draft


def run_case(orders_rows, customer_rows, change=None, **kwargs):
    orders, customers, draft = example()
    if change:
        change(draft)
    with MemorySource([orders], {orders.qualified_name: orders_rows}) as a, MemorySource(
            [customers], {customers.qualified_name: customer_rows}) as b:
        return execute_draft(draft, [a, b], [orders, customers], **kwargs)


def endpoint_pairs(result):
    nodes = {n['id']: n['identity']['id'] for n in result['graph']['nodes']}
    return {(nodes[e['source']], nodes[e['target']]) for e in result['graph']['edges']}


def test_cross_source_lookup_resolves_actual_customer_ids_without_cross_product():
    result = run_case([('O1', 'a@example', 'public'), ('O2', 'b@example', 'public')],
        [('C1', 'a@example', '甲', 'public'), ('C2', 'b@example', '乙', 'public')])
    assert result['passed'], result['diagnostics']
    assert endpoint_pairs(result) == {('O1', 'C1'), ('O2', 'C2')}
    for edge in result['graph']['edges']:
        match = edge['supports'][0]['lookup_matches'][0]
        assert match['source']['source_id'] == 'crm'
        assert match['input_values'] == [match['row_context']['values']['email']]
        assert match['resolved_key'] == [match['row_context']['values']['customer_id']]


def test_explicit_lookup_cannot_be_satisfied_by_a_fact_defined_phantom_target():
    def change(draft):
        draft['entities'][1]['mappings'].append({'source_id': 'orders', 'table': 'orders',
                                               'key_columns': ['email']})
    result = run_case([('O1', 'D404', 'public')], [('D1', 'known', '已知', 'public')], change)
    assert not result['passed']
    assert any(n['identity']['id'] == 'D404' for n in result['graph']['nodes'])
    assert result['graph']['edges'] == []
    assert 'lookup_reference_unmatched' in {d['code'] for d in result['diagnostics']}


@pytest.mark.parametrize('many', [False, True])
def test_ambiguous_matches_require_explicit_many_cardinality(many):
    def change(draft):
        draft['relations'][0]['mappings'][0]['target_lookup']['cardinality'] = 'many' if many else 'one'
    result = run_case([('O1', 'shared', 'public')],
        [('C1', 'shared', '甲', 'public'), ('C2', 'shared', '乙', 'public')], change)
    if many:
        assert result['passed']
        assert endpoint_pairs(result) == {('O1', 'C1'), ('O1', 'C2')}
    else:
        assert not result['passed']
        assert 'lookup_reference_ambiguous' in {d['code'] for d in result['diagnostics']}


def test_duplicate_matches_to_same_identity_are_not_ambiguous():
    result = run_case([('O1', 'same', 'public')],
        [('C1', 'same', '甲', 'public'), ('C1', 'same', '甲', 'public')])
    assert result['passed'] and endpoint_pairs(result) == {('O1', 'C1')}


def test_source_lookup_converts_input_key_without_changing_declared_identity():
    def change(draft):
        mapping = draft['relations'][0]['mappings'][0]
        mapping['source_key_columns'] = ['email']
        mapping['source_lookup'] = {'source_id': 'orders', 'table': 'orders',
                                    'match_columns': ['email'], 'key_columns': ['order_id']}
    result = run_case([('O1', 'a', 'public')], [('C1', 'a', '甲', 'public')], change)
    assert result['passed'] and endpoint_pairs(result) == {('O1', 'C1')}
    support = result['graph']['edges'][0]['supports'][0]
    assert {m['endpoint'] for m in support['lookup_matches']} == {'source', 'target'}
    assert {w['source_id'] for w in result['source_read_windows']} == {'orders', 'crm'}
    assert all(w['state'] == 'closed' and w['requested_at'] <= w['opened_at'] <= w['closed_at']
               for w in result['source_read_windows'])


def test_lookup_predicate_is_applied_to_actual_matching_rows():
    def change(draft):
        draft['relations'][0]['mappings'][0]['target_lookup']['predicate'] = {
            'column': 'scope', 'op': 'eq', 'value': 'B'}
    result = run_case([('O1', 'a', 'public')],
                     [('C1', 'a', '甲', 'A'), ('C2', 'a', '乙', 'B')], change)
    assert result['passed'] and endpoint_pairs(result) == {('O1', 'C2')}


def test_missing_match_may_be_explicitly_omitted_and_counted():
    def change(draft):
        draft['relations'][0]['mappings'][0]['target_lookup']['on_missing'] = 'omit'
    result = run_case([('O1', 'missing', 'public'), ('O2', None, 'public')],
        [('C1', 'known', '甲', 'public')], change)
    assert result['passed'] and not result['graph']['edges']
    assert result['metrics']['lookup_inputs_omitted'] == 2


@pytest.mark.parametrize('target_lookup', [True, False], ids=['unmatched_lookup', 'null_direct_key'])
def test_omitted_source_skips_unused_target_resolution(target_lookup):
    def change(draft):
        mapping = draft['relations'][0]['mappings'][0]
        mapping['source_lookup'] = {'source_id': 'orders', 'table': 'orders',
            'match_columns': ['order_id'], 'key_columns': ['order_id'],
            'predicate': {'column': 'scope', 'op': 'eq', 'value': 'other'}, 'on_missing': 'omit'}
        if not target_lookup:
            mapping['target_lookup'] = None
    result = run_case([('O1', None, 'public')], [('C1', 'known', '甲', 'public')], change)
    assert result['passed'], result['diagnostics']
    assert result['graph']['edges'] == []
    assert result['metrics']['lookup_inputs_omitted'] == 1


def test_composite_match_requires_tenant_and_preserves_numeric_types():
    def change(draft):
        mapping = draft['relations'][0]['mappings'][0]
        mapping['target_key_columns'] = ['scope', 'email']
        mapping['target_lookup']['match_columns'] = ['scope', 'email']
    result = run_case([('O1', 'shared', 'A'), ('O2', 'shared', 'B')],
        [('C1', 'shared', '甲', 'A'), ('C2', 'shared', '乙', 'B')], change)
    assert result['passed'] and endpoint_pairs(result) == {('O1', 'C1'), ('O2', 'C2')}
    mismatch = run_case([('O1', '1', 'public')], [('C1', 1, '甲', 'public')])
    assert not mismatch['passed']


def test_self_join_resolves_parent_id_from_different_row_with_renamed_columns():
    table = TableSpec(source_id='s', name='renamed', columns=[ColumnSpec(name=c)
        for c in ['x', 'y', 'z']])
    draft = {'draft_id': 'self', 'entities': [{'type': 'Item', 'key_names': ['id'], 'mappings': [
        {'source_id': 's', 'table': 'renamed', 'key_columns': ['x']}]}],
        'relations': [{'type': 'parent', 'source_type': 'Item', 'target_type': 'Item', 'acyclic': True,
            'mappings': [{'source_id': 's', 'table': 'renamed', 'source_key_columns': ['x'],
                         'target_key_columns': ['z'], 'target_lookup': {'source_id': 's',
                             'table': 'renamed', 'match_columns': ['y'], 'key_columns': ['x'], 'on_missing': 'omit'}}]}]}
    with MemorySource([table], {table.qualified_name: [('I1', 'root', None), ('I2', 'leaf', 'root')]}) as source:
        result = execute_draft(draft, [source], [table])
    assert result['passed'] and endpoint_pairs(result) == {('I2', 'I1')}


def test_lookup_authority_scope_cannot_leak_via_another_visible_target_support():
    def change(draft):
        draft['entities'][1]['mappings'].append({'source_id': 'orders', 'table': 'orders',
                                               'key_columns': ['email']})
    scopes = [{'source_id': 'orders', 'table': 'orders', 'columns': ['scope']},
              {'source_id': 'crm', 'table': 'customers', 'columns': ['scope']}]
    result = run_case([('O1', 'C1', 'public')], [('C1', 'C1', '秘密名', 'private')], change,
                      scope_bindings=scopes)
    assert result['passed'] and len(result['graph']['edges']) == 1
    restricted = filter_graph_scopes(result['graph'], ['public'])
    assert len(restricted['nodes']) == 2  # The target really has another public support.
    assert restricted['edges'] == [] and '秘密名' not in str(restricted)
    assert len(filter_graph_scopes(result['graph'], ['public', 'private'])['edges']) == 1


def test_lookup_columns_and_predicates_obey_goal_exclusions():
    result = run_case([('O1', 'a', 'public')], [('C1', 'a', '甲', 'public')],
        goal=ExtractionGoal(excluded_columns=['email']))
    assert not result['passed'] and result['graph']['edges'] == []
    assert 'excluded_column_projected' in {d['code'] for d in result['diagnostics']}


def test_identical_projections_reuse_one_snapshot_but_do_not_cache_across_runs():
    table = TableSpec(source_id='s', name='objects', columns=[ColumnSpec(name='id')])
    mapping = {'source_id': 's', 'table': 'objects', 'key_columns': ['id']}
    draft = {'draft_id': 'cache', 'entities': [{'type': 'Item', 'key_names': ['id'],
        'mappings': [mapping, {**mapping, 'constant_properties': {'position': 2}}]}]}
    with MemorySource([table], {table.qualified_name: [('A',)]}) as source:
        first = execute_draft(draft, [source], [table])
        queries = source.stats.queries
        second = execute_draft(draft, [source], [table])
        assert source.stats.queries == queries * 2
    assert first['passed'] and second['passed']
    assert first['metrics']['projection_cache_hits'] == 1
    assert first['metrics']['unique_projections'] == 1


def test_lookup_arity_is_checked_against_input_and_resolved_identity_separately():
    _, _, draft = example()
    draft['relations'][0]['mappings'][0]['target_lookup']['match_columns'] = ['email', 'scope']
    with pytest.raises(ValidationError, match='lookup_match_arity_mismatch'):
        GraphDraft.model_validate(draft)
    draft['relations'][0]['mappings'][0]['target_lookup']['match_columns'] = ['email']
    draft['relations'][0]['mappings'][0]['target_lookup']['key_columns'] = ['customer_id', 'scope']
    with pytest.raises(ValidationError, match='relation_key_arity_mismatch'):
        GraphDraft.model_validate(draft)


def test_lookup_source_fact_permission_is_checked_before_any_projection(tmp_path):
    orders, customers, draft = example()
    # Referencing a forbidden lookup must not be hidden behind allowed entity mappings.
    draft['entities'][1]['mappings'] = [{'source_id': 'orders', 'table': 'orders', 'key_columns': ['email']}]
    config = RunConfig(sources=[{'id': sid, 'kind': 'mysql_database', 'allowed_tables': [table],
        'allow_fact_data': enabled} for sid, table, enabled in [
            ('orders', 'public.orders', True), ('crm', 'public.customers', False)]],
        model={'mode': 'in_environment'})
    with MemorySource([orders], {orders.qualified_name: [('O1', 'C1', 'public')]}) as a, MemorySource(
            [customers], {customers.qualified_name: [('C1', 'C1', '甲', 'public')]}) as b:
        tools = ConstructionTools(config, tmp_path / 'work', adapters=[a, b])
        try:
            assert tools.call('save_graph_draft', yaml_text=dump_yaml(draft))['status'] == 'ok'
            response = tools.call('validate_graph_draft', draft_id='lookup')
            assert response['code'] == 'source_fact_data_forbidden'
            assert a.stats.queries == b.stats.queries == 0
        finally:
            tools.close()


def test_visible_relation_details_expose_actual_lookup_pair_evidence(monkeypatch):
    from lightgraph.web import construction_graph as views
    result = run_case([('O1', 'a@example', 'public')], [('C1', 'a@example', '甲', 'public')])
    graph = filter_graph_scopes(result['graph'], ['public'])
    for node in graph['nodes']:
        node['kind'] = node['type']
    for edge in graph['edges']:
        edge['relation'] = edge['type']
    monkeypatch.setattr(views, 'load_graph', lambda config: graph)
    edge_id = graph['edges'][0]['id']
    details = views.graph_details(None, fact_id=edge_id)
    binding = next(s['binding'] for s in details['supports'] if s['fact_id'] == edge_id)
    assert binding['row_context']['values']['order_id'] == 'O1'
    assert binding['lookup_matches'][0]['resolved_key'] == ['C1']
    assert binding['required_scope_refs'] == ['public']
    assert details['rules'][0]['endpoint_lookups']['target_lookup']['table'] == 'customers'


def test_inherited_lookup_uses_exact_support_and_published_id():
    table, _, draft = example()
    draft['entities'] = draft['entities'][:1]
    draft.update(extends_base_graph=True, inherited_entity_types=['Customer'])
    lookup = draft['relations'][0]['mappings'][0]['target_lookup']
    lookup.update(source_id='__base_graph__', table='Customer', match_columns=['label'], key_columns=['id'])
    # A merged unit can already have a namespaced ID. The name which justifies
    # this match exists only on B, although the same customer is visible on A.
    base = {'definitions': {'entities': [{'type': 'Customer', 'key_names': ['id']}]},
            'nodes': [{'id': 'n_published', 'type': 'Customer', 'identity': {'id': '1.00'},
                       'identity_types': {'id': 'decimal'},
                       'supports': [{'scope_ref': 'A', 'label': 'first'},
                                    {'scope_ref': 'B', 'required_scope_refs': ['C'], 'label': 'other'}]}]}
    with MemorySource([table], {table.qualified_name: [('O1', 'other', 'public')]}) as src:
        result = execute_draft(draft, [src], [table], base_graph=base)
    assert result['passed'], result['diagnostics']
    edge, = result['graph']['edges']
    assert edge['target'] == 'n_published'
    witness, = edge['supports']
    assert witness['required_scope_refs'] == ['B', 'C']
    assert witness['lookup_matches'][0]['resolved_key_types'] == ['decimal']
    for scopes, expected in [(['public', 'A'], 0), (['public', 'B'], 0), (['public', 'B', 'C'], 1)]:
        view = filter_graph_scopes(result['graph'], scopes, external_visible_ids={'n_published'})
        assert len(view['edges']) == expected


def test_inherited_relation_only_plan_checks_real_key_arity_in_both_modes():
    from lightgraph.construction.structure import validate_structure

    table, _, draft = example()
    draft.update(entities=[], extends_base_graph=True, inherited_entity_types=['Order', 'Customer'])
    mapping = draft['relations'][0]['mappings'][0]
    del mapping['target_lookup']
    base = {'definitions': {'entities': [{'type': kind, 'key_names': ['id']} for kind in ('Order', 'Customer')]},
            'nodes': [{'id': kind + '_from_a_unit', 'type': kind, 'identity': {'id': value},
                       'supports': [{'scope_ref': 'public'}]} for kind, value in [('Order', 'O1'), ('Customer', 'C1')]]}
    assert validate_structure(draft, [table], base_graph=base)['passed']
    with MemorySource([table], {table.qualified_name: [('O1', 'C1', 'public')]}) as src:
        result = execute_draft(draft, [src], [table], base_graph=base)
    assert result['passed'] and result['graph']['edges'][0]['target'] == 'Customer_from_a_unit'

    mapping['target_key_columns'] = ['email', 'scope']
    assert validate_structure(draft, [table], base_graph=base)['diagnostics'][0]['code'] == 'relation_key_arity_mismatch'
