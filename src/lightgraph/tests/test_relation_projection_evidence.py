"""Production adapters produce tuple evidence; DB-API fixtures are not live server qualification."""
from contextlib import contextmanager
from copy import deepcopy

import pytest

from lightgraph.construction.executor import execute_draft
from lightgraph.construction.quality import relation_evidence_advice
from lightgraph.construction.relation_assertions import assertion_signature, support_assertion_checks
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import dump_yaml
from lightgraph.runtime.config import RunConfig
from lightgraph.sources.csv import CSVSource
from lightgraph.sources.gauss import GaussSource
from lightgraph.sources.mysql import MySQLSource
from lightgraph.tests.test_construction_mixed_sources import database_transport


def draft_for(source_id, schema='public', *, context=None):
    origin = {'source_id': source_id, 'schema': schema, 'table': 'items'}
    mapping = {**origin, 'source_key_columns': ['code'], 'target_key_columns': ['parent_code'],
               'equal_endpoints': 'omit', 'business_context': context or {'status': 'global'}}
    return {'draft_id': 'relations', 'entities': [{'type': 'Item', 'key_names': ['code'],
        'mappings': [{**origin, 'key_columns': ['code'], 'label_column': 'name'}]}],
        'relations': [{'type': kind, 'source_type': 'Item', 'target_type': 'Item',
            'description': '同一目录内的直接上级关系。', 'hierarchy_orientation': 'child_to_parent',
            'mappings': [deepcopy(mapping)]} for kind in ('reports_to', 'catalog_parent')]}


@contextmanager
def source(tmp_path, kind):
    if kind == 'csv':
        path = tmp_path / 'items.csv'
        path.write_text('code,name,parent_code,tenant\nD1,部门甲,D2,A\nD2,部门乙,D2,A\n', encoding='utf-8')
        with CSVSource(path, source_id='local', encoding='utf-8', delimiter=',', header=True,
                       null_values=[''], type_inference='text') as adapter:
            yield adapter, 'local', 'public'
    else:
        schema, data, connection, factory, qualification = database_transport(kind)
        data.execute('PRAGMA query_only=OFF')
        data.execute(f"UPDATE {schema}.items SET parent_code = 'D2'")
        data.commit()
        data.execute('PRAGMA query_only=ON')
        if kind == 'gauss':
            original_factory = factory

            def factory(*args, **kwargs):
                opened = original_factory(*args, **kwargs)
                original_cursor = opened.cursor

                def cursor(*args, **kwargs):
                    result = original_cursor(*args, **kwargs)
                    execute = result.execute

                    def execute_with_catalog(sql, parameters=None):
                        execute(sql, parameters)
                        if 'information_schema.columns' in sql:
                            result.result = [(name, 'text', 'NO' if name == 'code' else 'YES')
                                             for name in ('tenant', 'code', 'name', 'parent_code')]

                    result.execute = execute_with_catalog
                    return result

                opened.cursor = cursor
                return opened
        adapter_class = MySQLSource if kind == 'mysql' else GaussSource
        dsn = 'mysql://tester:secret@localhost/analytics' if kind == 'mysql' else 'test-dsn'
        with adapter_class(dsn, source_id='db', allowed_tables=[f'{schema}.items'],
                           verified_capabilities=qualification, connect_factory=factory) as adapter:
            yield adapter, 'db', schema
        data.close()


@pytest.mark.parametrize('kind', ['csv', 'mysql', 'gauss'])
@pytest.mark.parametrize('context', [{'status': 'global'}, {'status': 'scoped', 'columns': {'project': 'tenant'}}])
def test_complete_projection_facts_have_source_bound_tuple_witnesses(tmp_path, kind, context):
    with source(tmp_path, kind) as (adapter, source_id, schema):
        draft = draft_for(source_id, schema, context=context)
        result = execute_draft(draft, [adapter], adapter.describe())
    assert result['passed'], result['diagnostics']
    assert len(result['graph']['edges']) == 2
    signatures = []
    definitions = {d['type']: d for d in result['graph']['definitions']['relations']}
    for edge in result['graph']['edges']:
        support, = edge['supports']
        witness = support['source_projection_witness']
        assert witness['kind'] == 'source_projection_v1' and witness['complete']
        assert not {'rows', 'snapshot', 'physical_row'} & witness.keys()
        check = support_assertion_checks(support)
        assert not check['blockers'], check
        reason, signature, _ = assertion_signature(edge, definitions[edge['type']])
        assert reason is None
        signatures.append(signature)
    assert len(set(signatures)) == 1
    changed = deepcopy(result['graph']['edges'][0])
    changed['supports'][0]['source_projection_witness']['source_binding'] = 'f' * 64
    assert assertion_signature(changed, definitions[changed['type']])[1] != signatures[0]


def test_csv_witness_and_scope_advice_survive_validation_publication_and_reuse(tmp_path):
    path = tmp_path / 'items.csv'
    path.write_text('code,name,parent_code,tenant\nD1,部门甲,D2,A\nD2,部门乙,D2,A\n', encoding='utf-8')
    config = RunConfig(sources=[{'id': 'local', 'kind': 'csv_file', 'path': str(path), 'encoding': 'utf-8',
        'delimiter': ',', 'header': True, 'null_values': [''], 'type_inference': 'text'}],
        model={'mode': 'in_environment'})
    draft = draft_for('local', context={'status': 'unknown'})
    tools = ConstructionTools(config, tmp_path / 'work')
    try:
        assert tools.save_graph_draft(dump_yaml(draft))['status'] == 'ok'
        for _ in range(2):
            result = tools.validate_graph_draft(draft['draft_id'])
            assert result['passed'], result
            assert 'relation_business_scope_unknown' in {a['code'] for a in result['quality_advice']}
        assert tools.publish_graph_draft(draft['draft_id'])['status'] == 'ok'
        graph = tools.workspace.read_published_graph()
        assert len(graph['edges']) == 2
        for edge in graph['edges']:
            checks = support_assertion_checks(edge['supports'][0])
            assert checks['witness_kind'] == 'source_projection_v1'
            assert checks['blockers'] == ['unknown_access_or_business_scope']
    finally:
        tools.close()


def test_independent_blockers_and_column_scope_null_are_not_hidden(tmp_path):
    with source(tmp_path, 'csv') as (adapter, source_id, schema):
        draft = draft_for(source_id, schema)
        result = execute_draft(draft, [adapter], adapter.describe())
    support = result['graph']['edges'][0]['supports'][0]
    del support['source_projection_witness']
    support['business_context'] = {'status': 'unknown', 'dimensions': {}}
    support['properties'] = {}
    support['provenance']['property_columns'] = {'amount': 'amount'}
    checks = support_assertion_checks(support)
    assert {'missing_or_invalid_physical_rows', 'unknown_access_or_business_scope',
            'unobserved_mapped_property'} <= set(checks['blockers'])
    support['provenance']['business_context_mapping'] = {'status': 'scoped', 'columns': {'project': 'tenant'}}
    advice = relation_evidence_advice(draft, result['graph'])[-1]
    assert advice['scoped_observations_with_unknown_values'] == 1


@pytest.mark.parametrize('mode', ['missing_cell', 'constant_dimension'])
def test_executor_does_not_confuse_missing_column_values_with_global_scope(tmp_path, mode):
    path = tmp_path / 'items.csv'
    path.write_text('code,name,parent_code,tenant\nD1,部门甲,D2,\nD2,部门乙,D2,A\n', encoding='utf-8')
    context = ({'status': 'scoped', 'columns': {'project': 'tenant'}} if mode == 'missing_cell'
               else {'status': 'scoped', 'values': {'project': 'A'}})
    draft = draft_for('local', context=context)
    with CSVSource(path, source_id='local', encoding='utf-8', delimiter=',', header=True,
                   null_values=[''], type_inference='text') as adapter:
        result = execute_draft(draft, [adapter], adapter.describe())
    assert result['passed'], result['diagnostics']
    assert len(result['graph']['edges']) == 2
    for edge in result['graph']['edges']:
        support, = edge['supports']
        checks = support_assertion_checks(support)
        if mode == 'missing_cell':
            assert support['business_context']['status'] == 'unknown'
            assert checks['blockers'] == ['unknown_access_or_business_scope']
        else:
            assert support['business_context']['status'] == 'scoped'
            assert not checks['blockers'], checks
    advice = relation_evidence_advice(draft, result['graph'])
    if mode == 'missing_cell':
        assert advice[-1]['scoped_observations_with_unknown_values'] == 2
    else:
        assert not advice
