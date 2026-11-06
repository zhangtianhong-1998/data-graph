"""Catalog definitions and evidence-backed row associations, not numeric instances."""
from copy import deepcopy
from contextlib import contextmanager
import re

import pytest
from pydantic import ValidationError

from lightgraph.contracts import ColumnSpec, TableSpec, ExtractionGoal
from lightgraph.sources import MemorySource
from lightgraph.runtime.config import RunConfig
from lightgraph.construction.access import filter_graph_scopes
from lightgraph.construction.executor import execute_draft
from lightgraph.construction.models import GraphDraft, COLUMN_IDENTITY_NAMES
from lightgraph.construction.structure import validate_structure
from lightgraph.construction.tools import ConstructionTools, SKILL_ROOT
from lightgraph.construction.workspace import dump_yaml, safe_load_yaml
from lightgraph.errors import LightGraphError


def physical_table(source='facts', name='observations'):
    return TableSpec(source_id=source, name=name, columns=[
        ColumnSpec(name=c) for c in ('indicator_code', 'indicator_name', 'office_code', 'office_name', 'scope')]
        + [ColumnSpec(name='comparison_total', data_type='NUMERIC', comment='去年同期季度完成')])


def column_mapping(ref='e1', **updates):
    return dict(kind='metadata_column', source_id='facts', table='observations',
                column='comparison_total', evidence_refs=[ref], **updates)


def column_draft(ref='e1'):
    return {'draft_id': 'analysis', 'entities': [
        {'type': 'MeasureDefinition', 'key_names': COLUMN_IDENTITY_NAMES, 'mappings': [column_mapping(ref)]}]}


def row_draft(basis='schema_applies_to_rows', ref='e1'):
    raw = column_draft(ref)
    source = {'source_id': 'facts', 'table': 'observations'}
    raw['entities'].insert(0, {'type': 'Indicator', 'key_names': ['code'], 'mappings': [
        {**source, 'key_columns': ['indicator_code'], 'label_column': 'indicator_name'}]})
    raw['relations'] = [{'type': 'uses_measure', 'source_type': 'Indicator', 'target_type': 'MeasureDefinition',
        'mappings': [{**source, 'source_key_columns': ['indicator_code'], 'target_metadata': {
            **source, 'column': 'comparison_total', 'evidence_refs': [ref]}, 'metadata_basis': basis}]}]
    return raw


class TrackedSource(MemorySource):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.projections = []
        self.snapshots = 0

    def project(self, table, columns, **kwargs):
        self.projections.append((list(columns), kwargs.get('predicate')))
        return super().project(table, columns, **kwargs)

    @contextmanager
    def snapshot(self):
        self.snapshots += 1
        with super().snapshot() as value:
            yield value


def test_empty_catalog_column_publishes_without_fact_permission_or_snapshot(tmp_path):
    table = physical_table()
    config = RunConfig(sources=[{'id': 'facts', 'kind': 'gauss_database', 'allow_fact_data': False,
                                'allowed_tables': ['public.observations']}], model={'mode': 'in_environment'})
    with TrackedSource([table], {table.qualified_name: []}) as source:
        tools = ConstructionTools(config, tmp_path / 'work', adapters=[source])
        try:
            evidence = tools.call('inspect_source', table='facts:public.observations', operation='get_metadata')
            raw = column_draft(evidence['evidence_ref'])
            assert tools.call('save_graph_draft', yaml_text=dump_yaml(raw))['status'] == 'ok'
            structure = tools.call('validate_graph_draft', draft_id='analysis', mode='structure')
            assert structure['passed'] and structure['bindings'][0]['source_kind'] == 'metadata_column'
            result = tools.call('validate_graph_draft', draft_id='analysis')
            assert result['passed'], result
            assert result['metrics']['projected_distinct_rows'] == 0
            assert tools.call('publish_graph_draft', draft_id='analysis')['nodes'] == 1
            graph = tools.workspace.read_published_graph()
            node, = graph['nodes']
            assert node['identity'] == dict(zip(COLUMN_IDENTITY_NAMES, ['facts', 'public', 'observations', 'comparison_total']))
            assert node['label'] == '去年同期季度完成' and node['label_origin'] == 'metadata'
            assert node['properties']['data_type'] == ['NUMERIC']
            assert evidence['evidence_ref'] in graph['evidence_refs']
            assert source.stats.queries == source.snapshots == 0 and not source.projections
            # Source fact permission is still enforced for relations and row objects.
            assert tools.call('save_graph_draft', yaml_text=dump_yaml(row_draft(ref=evidence['evidence_ref'])))['status'] == 'ok'
            assert tools.call('validate_graph_draft', draft_id='analysis')['code'] == 'source_fact_data_forbidden'
        finally:
            tools.close()


@pytest.mark.parametrize('basis,expected', [('schema_applies_to_rows', {'I1', 'I2'}),
                                          ('observed_non_null', {'I1'})])
@pytest.mark.parametrize('reverse', [False, True])
def test_relations_bind_actual_rows_never_measure_values(basis, expected, reverse):
    table = physical_table()
    raw = row_draft(basis)
    if reverse:
        relation = raw['relations'][0]; mapping = relation['mappings'][0]
        relation['source_type'], relation['target_type'] = relation['target_type'], relation['source_type']
        mapping['source_metadata'] = mapping.pop('target_metadata')
        mapping['target_key_columns'] = mapping.pop('source_key_columns')
    with TrackedSource([table], {table.qualified_name: [
            ('I1', '指标甲', 'O1', '办事处甲', 'A', 97654321),
            ('I1', '指标甲', 'O1', '办事处甲', 'A', 97654322),
            ('I2', '指标乙', None, '无编码名称', 'B', None)]}) as source:
        result = execute_draft(raw, [source], [table])
        assert result['passed'], result['diagnostics']
        graph = result['graph']; by_id = {node['id']: node for node in graph['nodes']}
        end = 'target' if reverse else 'source'
        assert {by_id[edge[end]]['identity']['code'] for edge in graph['edges']} == expected
        assert len(graph['nodes']) == 3
        assert all('comparison_total' not in cols for cols, _ in source.projections)
        assert '9765432' not in dump_yaml(graph)
        for edge in graph['edges']:
            support = edge['supports'][0]['provenance']['metadata_endpoint']
            assert support['basis'] == basis
            assert support['meaning_origin'] == 'agent_declared'
            assert support['non_null_observed'] == (basis == 'observed_non_null')


def test_nullable_dimension_and_measure_relations_coexist_with_cross_source_lookup():
    table = physical_table()
    tree = TableSpec(source_id='tree', name='items', columns=[ColumnSpec(name=c) for c in ('code', 'label')])
    raw = row_draft('observed_non_null')
    raw['entities'][0]['mappings'] = [{'source_id': 'tree', 'table': 'items', 'key_columns': ['code'], 'label_column': 'label'}]
    raw['relations'][0]['mappings'][0]['source_lookup'] = {
        'source_id': 'tree', 'table': 'items', 'match_columns': ['code'], 'key_columns': ['code']}
    source_ref = {'source_id': 'facts', 'table': 'observations'}
    nonnull = {'column': 'office_code', 'op': 'is_not_null'}
    raw['entities'].append({'type': 'Office', 'key_names': ['code'], 'mappings': [
        {**source_ref, 'key_columns': ['office_code'], 'label_column': 'office_name', 'predicate': nonnull}]})
    raw['relations'].append({'type': 'measured_in', 'source_type': 'Indicator', 'target_type': 'Office', 'mappings': [
        {**source_ref, 'source_key_columns': ['indicator_code'], 'target_key_columns': ['office_code'], 'predicate': nonnull,
         'source_lookup': deepcopy(raw['relations'][0]['mappings'][0]['source_lookup'])}]})
    with TrackedSource([table], {table.qualified_name: [('I1', '甲', 'O1', '办事处甲', 'public', 700),
            ('I2', '乙', None, '缺失编码', 'public', None)]}) as facts, MemorySource([tree], {
                tree.qualified_name: [('I1', '指标甲'), ('I2', '指标乙'), ('I3', '仅在指标树')]}) as hierarchy:
        result = execute_draft(raw, [facts, hierarchy], [table, tree])
        assert result['passed'], result['diagnostics']
        graph = result['graph']; nodes = {n['id']: n for n in graph['nodes']}
        assert len(nodes) == 5 and len(graph['edges']) == 2
        assert {e['type'] for e in graph['edges']} == {'uses_measure', 'measured_in'}
        assert {nodes[e['source']]['identity']['code'] for e in graph['edges']} == {'I1'}
        assert any('lookup_matches' in support for e in graph['edges'] for support in e['supports'])
        assert all('comparison_total' not in cols for cols, _ in facts.projections)


def test_metadata_aliases_and_meaning_are_separate_from_catalog_observations():
    table = physical_table()
    raw = column_draft()
    raw['entities'][0]['mappings'][0].update(label='同期季度累计', aliases=['Prior comparison'],
        semantic_annotations={'interpretation': '聚合方式'})
    with TrackedSource([table], {table.qualified_name: []}) as source:
        result = execute_draft(raw, [source], [table])
        assert result['passed']
        node, = filter_graph_scopes(result['graph'], ['public'])['nodes']
        assert node['label'] == '同期季度累计' and node['label_origin'] == 'metadata_declared'
        assert node['properties']['catalog_comment'] == ['去年同期季度完成']
        assert {s['source_kind'] for s in node['supports']} == {'catalog', 'declared'}
        assert node['aliases'] == ['Prior comparison']
        assert filter_graph_scopes(result['graph'], ['A'])['nodes'] == []
        raw['entities'][0]['mappings'][0]['semantic_annotations']['data_type'] = 'fictional'
        result = execute_draft(raw, [source], [table])
        assert not result['passed']
        assert 'metadata_annotation_catalog_conflict' in {d['code'] for d in result['diagnostics']}


def test_scoped_row_relations_never_leak_through_public_column_nodes():
    table = physical_table()
    with TrackedSource([table], {table.qualified_name: [('I1', '甲', None, None, 'A', 1),
            ('I2', '乙', None, None, 'B', 2)]}) as source:
        result = execute_draft(row_draft(), [source], [table], scope_bindings=[
            {'source_id': 'facts', 'table': 'observations', 'columns': ['scope']}])
        assert result['passed']
        graph = filter_graph_scopes(result['graph'], ['public', 'A'])
        assert len(graph['nodes']) == 2 and len(graph['edges']) == 1
        assert {n['identity'].get('code') for n in graph['nodes']} == {'I1', None}
        public = filter_graph_scopes(result['graph'], ['public'])
        assert len(public['nodes']) == 1 and public['edges'] == []


@pytest.mark.parametrize('goal,code', [({'excluded_columns': ['comparison_total']}, 'excluded_column_projected'),
    ({'evidence_only_columns': ['comparison_total']}, 'evidence_only_column_projected'),
    ({'source_scope': ['other']}, 'outside_source_goal')])
def test_metadata_columns_obey_goal_authorization(goal, code):
    table = physical_table()
    with TrackedSource([table], {table.qualified_name: []}) as source:
        result = execute_draft(column_draft(), [source], [table], goal=ExtractionGoal(**goal))
        assert not result['passed'] and code in {d['code'] for d in result['diagnostics']}
        assert source.stats.queries == 0


@pytest.mark.parametrize('change,code', [
    (lambda d: d['entities'][0]['key_names'].clear(), 'too_short'),
    (lambda d: d['relations'][0]['mappings'][0].pop('metadata_basis'), 'metadata_relation_basis_required'),
    (lambda d: d['relations'][0]['mappings'][0]['target_metadata'].update(table='another'), 'metadata_relation_input_table_mismatch'),
    (lambda d: d['relations'][0]['mappings'][0].update(target_key_columns=['indicator_code']), 'ambiguous_relation_endpoint'),
    (lambda d: d['relations'][0]['mappings'][0]['target_metadata'].update(column='another'), 'metadata_endpoint_mapping_missing'),
])
def test_invalid_column_identity_and_relation_shapes_are_rejected(change, code):
    raw = row_draft(); change(raw)
    with pytest.raises(ValidationError, match=code):
        GraphDraft.model_validate(raw)


def test_same_named_columns_in_different_tables_are_independent():
    tables = [physical_table(name=name) for name in ('observations', 'second')]
    raw = column_draft()
    raw['entities'][0]['mappings'].append({**column_mapping(), 'table': 'second'})
    with TrackedSource(tables, {t.qualified_name: [] for t in tables}) as source:
        result = execute_draft(raw, [source], tables)
        assert result['passed'] and len(result['graph']['nodes']) == 2
        assert {n['identity']['table'] for n in result['graph']['nodes']} == {'observations', 'second'}
        assert source.stats.queries == 0


@pytest.mark.parametrize('column_name', ['甲字段', 'lyqtd'])
def test_renamed_column_and_empty_fact_table_need_no_business_name_template(column_name):
    table = physical_table()
    table.columns[-1] = table.columns[-1].model_copy(update={'name': column_name})
    raw = row_draft('observed_non_null')
    raw['entities'][1]['mappings'][0]['column'] = column_name
    raw['relations'][0]['mappings'][0]['target_metadata']['column'] = column_name
    with TrackedSource([table], {table.qualified_name: []}) as source:
        result = execute_draft(raw, [source], [table])
        assert result['passed']
        node, = result['graph']['nodes']
        assert node['identity']['column'] == column_name and node['label'] == '去年同期季度完成'
        assert result['graph']['edges'] == []
        assert all(column_name not in cols for cols, _ in source.projections)


def test_repeated_column_mapping_keeps_compatible_support_and_detects_real_conflicts():
    table = physical_table()
    raw = column_draft()
    raw['entities'][0]['mappings'].append(column_mapping(label='同期季度累计', aliases=['Prior'],
        semantic_annotations={'period': 'previous quarter'}))
    raw['entities'][0]['mappings'].append(deepcopy(raw['entities'][0]['mappings'][1]))
    with TrackedSource([table], {table.qualified_name: []}) as source:
        result = execute_draft(raw, [source], [table])
        assert result['passed'], result['diagnostics']
        node, = result['graph']['nodes']
        assert node['label'] == '同期季度累计'
        assert {s['provenance']['mapping'] for s in node['supports']} == {0, 1, 2}
        for field, value in [('label', '不兼容主名称'), ('semantic_annotations', {'period': 'current year'})]:
            conflict = deepcopy(raw)
            conflict['entities'][0]['mappings'][2][field] = value
            failed = execute_draft(conflict, [source], [table])
            assert not failed['passed']
            assert 'metadata_column_mapping_conflict' in {d['code'] for d in failed['diagnostics']}


def test_guide_example_parses_and_schema_exposes_new_operator():
    guide = (SKILL_ROOT / 'references' / 'metadata-objects.md').read_text(encoding='utf-8')
    block = re.findall(r'```yaml\n(.*?)```', guide, re.DOTALL)[0]
    draft = GraphDraft.model_validate(safe_load_yaml(block))
    assert validate_structure(draft, [physical_table()])['passed']
    assert 'MetadataColumnMapping' in GraphDraft.model_json_schema()['$defs']


def test_frontend_details_distinguish_catalog_declaration_and_row_relation_basis(monkeypatch):
    from lightgraph.web import construction_graph as views
    table = physical_table()
    raw = row_draft('observed_non_null')
    raw['entities'][1]['mappings'][0]['label'] = '同期季度累计'
    with MemorySource([table], {table.qualified_name: [('I1', '指标甲', None, None, 'public', 700)]}) as source:
        result = execute_draft(raw, [source], [table])
        assert result['passed']
        graph = filter_graph_scopes(result['graph'], ['public'])
        for node in graph['nodes']:
            node['kind'] = node['type']
        for edge in graph['edges']:
            edge['relation'] = edge['type']
        monkeypatch.setattr(views, 'load_graph', lambda config: graph)
        definition = next(node for node in graph['nodes'] if node['type'] == 'MeasureDefinition')
        details = views.graph_details(None, entity_id=definition['id'])
        existence = [s for s in details['supports'] if s['fact_id'] == definition['id']]
        assert {s['origin'] for s in existence} == {'metadata', 'agent_declared'}
        edge = next(s for s in details['supports'] if s['fact_id'] == graph['edges'][0]['id'])
        assert edge['origin'] == 'source_query'
        assert edge['binding']['metadata_endpoint']['basis'] == 'observed_non_null'
        assert 'e1' in dump_yaml(details) and '700' not in dump_yaml(details)


def test_publication_never_merges_column_identity_with_coincidental_row_values():
    from lightgraph.construction.workspace import _merge_graphs
    table = physical_table()
    rows_table = TableSpec(source_id='other', name='keys', columns=[ColumnSpec(name=c)
        for c in ['a', 'b', 'c', 'd', 'label']])
    row = {'draft_id': 'rows', 'entities': [{'type': 'MeasureDefinition', 'key_names': COLUMN_IDENTITY_NAMES,
        'mappings': [{'source_id': 'other', 'table': 'keys', 'key_columns': ['a', 'b', 'c', 'd'], 'label_column': 'label'}]}]}
    with MemorySource([table], {table.qualified_name: []}) as source, MemorySource([rows_table], {
            rows_table.qualified_name: [('facts', 'public', 'observations', 'comparison_total', '去年同期季度完成')]}) as rows:
        catalog_graph = execute_draft(column_draft(), [source], [table])['graph']
        row_graph = execute_draft(row, [rows], [rows_table])['graph']
        assert catalog_graph['nodes'][0]['id'] == row_graph['nodes'][0]['id']
        with pytest.raises(LightGraphError, match='published_definition_conflict'):
            _merge_graphs([catalog_graph, row_graph])
        # Historical graphs with no origin still mean row values.
        row_graph['definitions']['entities'][0].pop('identity_origin')
        with pytest.raises(LightGraphError, match='published_definition_conflict'):
            _merge_graphs([row_graph, catalog_graph])
        assert len(_merge_graphs([row_graph, deepcopy(row_graph)])['nodes']) == 1
        enriched = column_draft(); enriched['entities'][0]['mappings'][0].update(label='同期季度累计')
        declared_graph = execute_draft(enriched, [source], [table])['graph']
        merged = _merge_graphs([catalog_graph, declared_graph])
        assert len(merged['nodes']) == 1 and merged['nodes'][0]['label'] == '同期季度累计'


def test_issued_column_evidence_is_checked_for_kind_scope_and_document_revision(tmp_path):
    table = physical_table(); other = physical_table(name='other')
    document = tmp_path / 'definitions.md'
    document.write_text('---\nid: definitions\npurpose: data_description\nscope:\n  source_ids: [facts]\n'
        '  tables: [public.observations]\n  columns: [comparison_total]\n---\n# 定义\ncomparison_total 是去年同期季度完成。\n', encoding='utf-8')
    config = RunConfig(sources=[], model={'mode': 'in_environment'}, documents=[str(document)])
    with TrackedSource([table, other], {t.qualified_name: [] for t in (table, other)}) as source:
        tools = ConstructionTools(config, tmp_path / 'work', adapters=[source])
        try:
            metadata = tools.call('inspect_source', table='facts:public.other', operation='get_metadata')
            result = tools.call('save_graph_draft', yaml_text=dump_yaml(column_draft(metadata['evidence_ref'])))
            assert result['code'] == 'metadata_evidence_not_applicable'
            assert result['diagnostics'][0]['path'][-2:] == ['evidence_refs', 0]
            fake_validation = tools.workspace.issue_evidence('data_validation', details={'passed': True})
            assert tools.call('save_graph_draft', yaml_text=dump_yaml(column_draft(fake_validation.ref)))['code'] == 'metadata_evidence_not_applicable'
            assert tools.call('save_graph_draft', yaml_text=dump_yaml(column_draft('unissued')))['status'] == 'error'
            material = tools.call('read_material', name='document:definitions')
            assert material['status'] == 'ok' and material['section_fully_read'], material
            ref = next(r for r in material['evidence_refs'] if tools.workspace.read_evidence(r).kind == 'document_section')
            assert tools.call('save_graph_draft', yaml_text=dump_yaml(column_draft(ref)))['status'] == 'ok'
            raw = column_draft(ref); raw['entities'][0]['mappings'][0]['column'] = 'office_code'
            assert tools.call('save_graph_draft', yaml_text=dump_yaml(raw))['code'] == 'metadata_evidence_not_applicable'
            raw = row_draft(ref=ref)
            raw['relations'][0]['mappings'][0]['target_metadata']['evidence_refs'] = [metadata['evidence_ref']]
            rejected = tools.call('save_graph_draft', yaml_text=dump_yaml(raw))
            assert rejected['diagnostics'][0]['path'][-3:] == ['target_metadata', 'evidence_refs', 0]
            document.write_text(document.read_text(encoding='utf-8') + '\n修订说明', encoding='utf-8')
            assert tools.call('validate_graph_draft', draft_id='analysis')['code'] == 'construction_source_changed'
        finally:
            tools.close()
