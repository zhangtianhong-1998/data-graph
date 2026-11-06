"""Documented NULL predicates execute as authored without changing identity."""
from copy import deepcopy
import re

import pytest

from lightgraph.construction.executor import execute_draft
from lightgraph.construction.models import GraphDraft
from lightgraph.construction.structure import validate_structure
from lightgraph.construction.tools import ConstructionTools, SKILL_ROOT
from lightgraph.construction.workspace import safe_load_yaml
from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.errors import LightGraphError
from lightgraph.query import QueryCompiler
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource


def example():
    text = (SKILL_ROOT / 'references' / 'predicate-guide.md').read_text(encoding='utf-8')
    examples = [safe_load_yaml(block) for block in re.findall(r'```yaml\n(.*?)```', text, re.DOTALL)]
    return next(value for value in examples if 'draft_id' in value)


def table():
    return TableSpec(source_id='sample', name='facts', columns=[ColumnSpec(name=name) for name in
        ('metriccode', 'metricname', 'officecode', 'office_name')])


def rows():
    return [('M01', '指标甲', '001', '区域甲'), ('M02', '指标乙', '002', '区域乙'),
            ('M03', '指标丙', None, '名称存在但编码缺失')]


def execute(value, records):
    physical = table()
    with MemorySource([physical], {physical.qualified_name: records}) as source:
        return execute_draft(value, [source], [physical])


def test_predicate_reference_is_available_to_agent_and_its_full_yaml_is_valid(tmp_path):
    config = RunConfig(sources=[], model={'mode': 'in_environment'})
    with MemorySource([table()], {'public.facts': rows()}) as source:
        tools = ConstructionTools(config, tmp_path / 'work', adapters=[source])
        try:
            material = tools.call('read_material', name='reference:predicate-guide.md')
            assert material['status'] == 'ok'
            assert 'is_not_null' in material['text'] and 'null_identity_key' in material['text']
            model = GraphDraft.model_validate(example())
            assert model.entities[1].mappings[0].key_columns == ['officecode']
            assert model.entities[1].mappings[0].label_column == 'office_name'
        finally:
            tools.close()


def test_full_yaml_retains_nullable_dimension_and_keeps_all_metric_identities():
    value = example(); before = deepcopy(value)
    result = execute(value, rows())
    assert result['passed'], result['diagnostics']
    assert value == before
    nodes = result['graph']['nodes']; edges = result['graph']['edges']
    offices = [node for node in nodes if node['type'] == 'Office']
    assert {node['identity']['code'] for node in offices} == {'001', '002'}
    assert {node['label'] for node in offices} == {'区域甲', '区域乙'}
    assert len([node for node in nodes if node['type'] == 'Indicator']) == 3
    assert len(edges) == 2
    by_id = {node['id']: node for node in nodes}
    assert {(by_id[edge['source']]['identity']['code'], by_id[edge['target']]['identity']['code'])
            for edge in edges} == {('M01', '001'), ('M02', '002')}
    assert all(node['identity']['code'] != '名称存在但编码缺失' for node in nodes)


def test_no_predicate_does_not_implicitly_drop_null_key_or_replace_it_with_name():
    value = example()
    value['entities'][1]['mappings'][0].pop('predicate')
    value['relations'][0]['mappings'][0].pop('predicate')
    result = execute(value, rows())
    assert not result['passed'] and result['graph']['status'] == 'candidate'
    assert 'null_identity_key' in {item['code'] for item in result['diagnostics']}
    assert value['entities'][1]['mappings'][0]['key_columns'] == ['officecode']


def test_relation_requires_its_own_explicit_input_scope():
    value = example()
    value['relations'][0]['mappings'][0].pop('predicate')
    result = execute(value, rows())
    assert not result['passed']
    assert any(item['code'] == 'null_identity_key' and item.get('relation_type') == 'belongs_to'
               for item in result['diagnostics'])


def test_empty_string_is_not_null_and_requires_an_independently_declared_exclusion():
    records = rows() + [('M04', '指标丁', '', '空编码名称')]
    value = example()
    failed = execute(value, records)
    assert not failed['passed']
    assert 'null_identity_key' in {item['code'] for item in failed['diagnostics']}
    predicate = {'all': [{'column': 'officecode', 'op': 'is_not_null'},
                         {'column': 'officecode', 'op': 'ne', 'value': ''}]}
    value['entities'][1]['mappings'][0]['predicate'] = deepcopy(predicate)
    value['relations'][0]['mappings'][0]['predicate'] = deepcopy(predicate)
    value['decisions'].append('本测试明确授权仅区域实体和关系排除NULL及空字符串编码，指标仍全部保留。')
    result = execute(value, records)
    assert result['passed'], result['diagnostics']
    assert len([node for node in result['graph']['nodes'] if node['type'] == 'Indicator']) == 4
    assert len([node for node in result['graph']['nodes'] if node['type'] == 'Office']) == 2
    assert len(result['graph']['edges']) == 2


@pytest.mark.parametrize('operator,expected', [('is_null', {None}), ('is_not_null', {'', '001', ' '}),
                                              ('not_null', {'', '001', ' '})])
def test_null_predicates_do_not_trim_or_confuse_empty_strings(operator, expected):
    physical = table()
    records = [(str(index), 'name', code, 'office') for index, code in enumerate([None, '', '001', ' '])]
    with MemorySource([physical], {physical.qualified_name: records}) as source:
        values = []
        for batch in source.project(physical, ['officecode'], predicate={'column': 'officecode', 'op': operator}):
            with batch:
                values.extend(row[0] for row in batch)
    assert set(values) == expected


@pytest.mark.parametrize('dialect', ['sqlite', 'postgres', 'mysql'])
@pytest.mark.parametrize('operator', ['is_null', 'is_not_null', 'not_null'])
def test_null_predicate_compilation_is_readonly_in_every_supported_dialect(dialect, operator):
    physical = table(); compiler = QueryCompiler([physical], dialect=dialect)
    predicate = {'column': 'officecode', 'op': operator}
    queries = [compiler.projection(physical, ['officecode', 'office_name'], predicate=predicate),
               compiler.profile_column_group(physical, ['officecode'], predicate=predicate),
               compiler.check_fd(physical, ['officecode'], 'office_name', predicate=predicate)]
    for query in queries:
        assert compiler.guard.validate(query.sql, query.parameters)
        assert 'NULL' in query.sql and None not in query.parameters.values()
    report = validate_structure(GraphDraft.model_validate(example()), [physical], dialects={'sample': dialect})
    assert report['passed'], report['diagnostics']


@pytest.mark.parametrize('predicate,code', [
    ({'column': 'officecode', 'op': 'eq', 'value': None}, 'predicate_null_requires_explicit_operator'),
    ({'column': 'officecode', 'op': 'is_not_null', 'value': None}, 'invalid_predicate'),
    ({'column': 'unregistered', 'op': 'is_not_null'}, 'unauthorized_column'),
])
def test_null_conditions_preserve_typed_contract_and_column_authorization(predicate, code):
    with pytest.raises(LightGraphError, match=code):
        QueryCompiler([table()]).projection(table(), ['officecode'], predicate=predicate)


def test_predicate_scalar_sql_text_stays_bound_and_cannot_expand_access():
    compiler = QueryCompiler([table()])
    payload = "x'; DELETE FROM facts; --"
    query = compiler.projection(table(), ['officecode'], predicate={'column': 'officecode', 'op': 'eq', 'value': payload})
    assert payload not in query.sql and payload in query.parameters.values()
    assert compiler.guard.validate(query.sql, query.parameters)
