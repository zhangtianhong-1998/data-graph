"""Generic mappings execute identically after physical renaming; no legacy rules."""
from copy import deepcopy
import pytest
from lightgraph.contracts import ColumnSpec, TableSpec, ExtractionGoal, QueryLimits
from lightgraph.sources import MemorySource
from lightgraph.construction.models import GraphDraft
from lightgraph.construction.executor import execute_draft


def fixture(columns=('范围', '甲', '甲名', '乙', '乙名', '别名')):
    table = TableSpec(source_id='s', name='任意表', columns=[ColumnSpec(name=c) for c in columns])
    rows = [('S', '01', '甲对象', '02', '乙对象', 'B'), ('S', '01', '甲对象', '01', '甲对象', 'A')]
    scope, a, an, b, bn, alias = columns
    draft = GraphDraft.model_validate({'draft_id': '映射', 'entities': [{'type': '自定义对象',
        'key_names': ['范围', '编号'], 'mappings': [
            {'source_id': 's', 'table': table.name, 'key_columns': [scope, a], 'label_column': an},
            {'source_id': 's', 'table': table.name, 'key_columns': [scope, b], 'label_column': bn,
             'alias_columns': [alias], 'properties': {'英文': alias}}]}],
        'relations': [{'type': '组成', 'source_type': '自定义对象', 'target_type': '自定义对象',
                      'acyclic': True, 'mappings': [{'source_id': 's', 'table': table.name,
                      'source_key_columns': [scope, a], 'target_key_columns': [scope, b],
                      'equal_endpoints': 'omit'}]}]})
    return table, rows, draft


def test_multiplace_identity_and_renamed_fields():
    outputs = []
    for columns in [('范围', '甲', '甲名', '乙', '乙名', '别名'), ('tenant', 'x', 'y', 'z', 'w', 'v')]:
        table, rows, draft = fixture(columns)
        with MemorySource([table], {table.qualified_name: rows}) as adapter:
            result = execute_draft(draft, [adapter], [table])
        assert result['passed'], result['diagnostics']
        assert result['metrics']['nodes'] == 2 and result['metrics']['edges'] == 1
        assert result['metrics']['equal_endpoints_omitted'] == 1
        first = next(n for n in result['graph']['nodes'] if n['identity']['编号'] == '01')
        assert len(first['provenance']) == 2
        assert first['aliases'] == ['A'] and first['properties'] == {'英文': ['A']}
        outputs.append({n['id'] for n in result['graph']['nodes']})
    assert outputs[0] == outputs[1]


def test_same_key_different_label_is_conflict_and_retained_candidate():
    table, rows, draft = fixture()
    rows[1] = ('S', '01', '甲对象', '01', '另一个名字', 'A')
    with MemorySource([table], {table.qualified_name: rows}) as adapter:
        result = execute_draft(draft, [adapter], [table])
    assert not result['passed']
    assert 'same_identity_conflicting_label' in {d['code'] for d in result['diagnostics']}
    assert result['graph']['status'] == 'candidate'


def test_full_projection_truncation_and_filtered_predicate_permissions():
    table, rows, draft = fixture()
    with MemorySource([table], {table.qualified_name: rows}, limits=QueryLimits(max_result_rows=1)) as adapter:
        result = execute_draft(draft, [adapter], [table])
        assert not result['passed']
        assert 'projection_truncated' in {d['code'] for d in result['diagnostics']}
    data = draft.model_dump(mode='python', by_alias=True)
    data['relations'] = []
    data['entities'][0]['mappings'] = [data['entities'][0]['mappings'][0]]
    data['entities'][0]['mappings'][0]['predicate'] = {'column': '别名', 'op': 'eq', 'value': 'A'}
    filtered = table.model_copy(update={'columns': [c for c in table.columns if c.name != '别名']})
    with MemorySource([table], {table.qualified_name: rows}) as adapter:
        result = execute_draft(GraphDraft.model_validate(data), [adapter], [filtered])
    assert not result['passed']
    assert result['graph']['nodes'] == []


def test_cross_source_attribute_increment_and_scope_identity():
    table, rows, draft = fixture()
    more = TableSpec(source_id='other', name='附加', columns=[ColumnSpec(name=c) for c in ('场所', '编号', '描述')])
    data = draft.model_dump(mode='python', by_alias=True)
    data['entities'][0]['mappings'].append({'source_id': 'other', 'table': '附加',
        'key_columns': ['场所', '编号'], 'properties': {'补充': '描述'}})
    with MemorySource([table], {table.qualified_name: rows}) as left, MemorySource([more],
            {more.qualified_name: [('S', '01', '补充信息'), ('T', '01', '另一范围')]}) as right:
        result = execute_draft(GraphDraft.model_validate(data), [left, right], [table, more])
    assert result['passed'], result['diagnostics']
    assert len(result['graph']['nodes']) == 3
    shared = next(n for n in result['graph']['nodes'] if n['identity'] == {'范围': 'S', '编号': '01'})
    assert shared['properties']['补充'] == ['补充信息']
    assert len(shared['provenance']) == 3


def test_goal_evidence_only_and_entity_scope_are_enforced():
    table, rows, draft = fixture()
    with MemorySource([table], {table.qualified_name: rows}) as adapter:
        result = execute_draft(draft, [adapter], [table], goal=ExtractionGoal(evidence_only_columns=['甲']))
        assert 'evidence_only_column_projected' in {d['code'] for d in result['diagnostics']}
        restricted = execute_draft(draft, [adapter], [table], goal=ExtractionGoal(entity_kinds=['其他类型']))
        assert 'outside_extraction_goal' in {d['code'] for d in restricted['diagnostics']}
        assert not restricted['passed']


def test_goal_exclusion_applies_to_predicate_columns_with_full_catalog():
    table, rows, draft = fixture()
    data = draft.model_dump(mode='python', by_alias=True)
    data['relations'] = []
    data['entities'][0]['mappings'] = [data['entities'][0]['mappings'][0]]
    data['entities'][0]['mappings'][0]['predicate'] = {'column': '别名', 'op': 'eq', 'value': 'A'}
    with MemorySource([table], {table.qualified_name: rows}) as adapter:
        result = execute_draft(GraphDraft.model_validate(data), [adapter], [table],
                               goal=ExtractionGoal(excluded_columns=['别名']))
        assert not result['passed'] and not result['graph']['nodes']
        assert adapter.stats.queries == 0


def test_cycle_and_memory_limit_do_not_produce_publishable_graphs():
    table, _, draft = fixture()
    rows = [('S', '01', '甲对象', '02', '乙对象', 'B'),
            ('S', '02', '乙对象', '01', '甲对象', 'A')]
    with MemorySource([table], {table.qualified_name: rows}) as adapter:
        result = execute_draft(draft, [adapter], [table])
        assert not result['passed']
        assert 'relation_cycle' in {d['code'] for d in result['diagnostics']}
        limited = execute_draft(draft, [adapter], [table], max_working_bytes=1)
        assert not limited['passed'] and limited['graph']['nodes'] == []


def test_snapshot_exit_failure_and_incomplete_projection_fail_validation():
    from contextlib import contextmanager
    from lightgraph.errors import LightGraphError
    table, rows, draft = fixture()
    class BrokenSnapshot(MemorySource):
        @contextmanager
        def snapshot(self):
            with super().snapshot():
                yield self
                raise LightGraphError('snapshot_lost')
    with BrokenSnapshot([table], {table.qualified_name: rows}) as adapter:
        result = execute_draft(draft, [adapter], [table])
        assert not result['passed']
        assert 'snapshot_lost' in {d['code'] for d in result['diagnostics']}
    class MissingProjection(MemorySource):
        def project(self, *args, **kwargs):
            return iter(())
    with MissingProjection([table], {table.qualified_name: rows}) as adapter:
        result = execute_draft(draft, [adapter], [table])
        assert not result['passed']
        assert 'projection_incomplete' in {d['code'] for d in result['diagnostics']}
