"""Structure-only output must not turn metadata checks into fact acceptance."""
import pytest

from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.construction.runner import run_construction
from lightgraph.construction.structure import structure_ready
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import dump_yaml, safe_load_yaml, write_yaml
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource


@pytest.fixture
def environment(tmp_path):
    class MetadataOnly(MemorySource):
        def execute(self, *args, **kwargs):
            raise AssertionError('structure checks may not execute a query')

        def project(self, *args, **kwargs):
            raise AssertionError('structure checks may not enumerate facts')

    table = TableSpec(source_id='s', name='items', columns=[ColumnSpec(name=c) for c in ['code', 'name', 'parent']])
    source = MetadataOnly([table], {table.qualified_name: []})
    config = RunConfig(sources=[{'id': 's', 'kind': 'gauss_database', 'allow_fact_data': False,
                                'allowed_tables': ['public.items']}],
                       model={'mode': 'metadata_only', 'model': 'scripted-test', 'base_url': 'http://127.0.0.1:9999/v1'}, construction_target='structure',
                       output_dir=str(tmp_path / 'output'))
    draft = {'draft_id': 'items', 'entities': [{'type': 'Item', 'key_names': ['code'],
        'mappings': [{'source_id': 's', 'table': 'items', 'key_columns': ['code'], 'label_column': 'name'}]}],
        'relations': [{'type': 'parent', 'source_type': 'Item', 'target_type': 'Item',
            'mappings': [{'source_id': 's', 'table': 'items', 'source_key_columns': ['parent'],
                         'target_key_columns': ['code'], 'source_lookup': {'source_id': 's', 'table': 'items',
                         'match_columns': ['code'], 'key_columns': ['code']}}]}]}
    tools = ConstructionTools(config, tmp_path / 'output' / 'construction', adapters=[source])
    tools.save_graph_draft(dump_yaml(draft))
    try:
        yield config, source, tools, draft
    finally:
        tools.close()
        source.close()


def test_structure_check_and_publication_use_no_fact_permission_or_queries(environment):
    _, source, tools, _ = environment
    check = tools.call('validate_graph_draft', draft_id='items')
    assert check['passed'] and check['data_validation'] == 'not_run'
    assert check['metrics']['fact_queries'] == 0
    assert tools.workspace.get_draft('items').validation is None
    publication = tools.call('publish_graph_draft', draft_id='items')
    assert publication['status'] == 'ok' and publication['target'] == 'graph_structure'
    assert publication['business_nodes'] == 0
    assert tools.workspace.read_published_graph()['nodes'] == []
    assert structure_ready(tools.workspace, tools.workspace.get_draft('items'))
    assert tools.call('validate_graph_draft', draft_id='items', mode='instances')['code'] == 'metadata_only_query_forbidden'
    assert source.stats.queries == 0


@pytest.mark.parametrize('change', ['field', 'lookup', 'predicate'])
def test_structure_checks_all_bindings_and_predicates_before_queries(environment, change):
    _, source, tools, draft = environment
    if change == 'field':
        draft['entities'][0]['mappings'][0]['label_column'] = 'absent'
    elif change == 'lookup':
        draft['relations'][0]['mappings'][0]['source_lookup']['table'] = 'secret'
    else:
        draft['relations'][0]['mappings'][0]['source_lookup']['predicate'] = {'column': 'secret', 'op': 'eq', 'value': 'x'}
    tools.save_graph_draft(dump_yaml(draft))
    result = tools.call('validate_graph_draft', draft_id='items')
    assert result['status'] == 'ok' and not result['passed']
    assert result['diagnostics'][0]['code'] in {'unauthorized_column', 'unauthorized_object'}
    assert tools.call('publish_graph_draft', draft_id='items')['code'] == 'structure_validation_required'
    assert source.stats.queries == 0


def test_structure_artifacts_are_versioned_and_tamper_checked(environment):
    _, _, tools, draft = environment
    first = tools.call('validate_graph_draft', draft_id='items')
    second = tools.call('validate_graph_draft', draft_id='items')
    assert first['structure_artifact'] != second['structure_artifact']
    assert (tools.workspace.root / first['structure_artifact']).exists()
    path = tools.workspace.root / second['structure_artifact']
    value = safe_load_yaml(path.read_text(encoding='utf-8'))
    value['draft']['purpose'] = 'tampered'
    write_yaml(path, value)
    assert tools.call('publish_graph_draft', draft_id='items')['code'] == 'validated_structure_mismatch'
    tools.call('validate_graph_draft', draft_id='items')
    assert tools.call('publish_graph_draft', draft_id='items')['status'] == 'ok'
    draft['purpose'] = 'new goal binding'
    tools.save_graph_draft(dump_yaml(draft))
    assert not structure_ready(tools.workspace, tools.workspace.get_draft('items'))
    assert tools.call('publish_graph_draft', draft_id='items')['code'] == 'structure_validation_required'


def test_structure_run_can_complete_without_claiming_verified_instances(environment, monkeypatch):
    config, source, tools, draft = environment
    monkeypatch.setattr('lightgraph.construction.runner.ConstructionTools', lambda *a, **kw: tools)
    def runtime(*args, **kwargs):
        assert tools.call('validate_graph_draft', draft_id='items')['passed']
        assert tools.call('publish_graph_draft', draft_id='items')['status'] == 'ok'
        return {'finish_reason': 'completed', 'budget': {'calls': 2}}
    result = run_construction(config, adapters=[source], runtime=runtime)
    assert result.status == 'COMPLETED', result.model_dump()
    assert result.counts['business_nodes'] == 0
    assert result.validation['business_graph'] == 'not_requested'
    assert result.construction.execution['data_validation'] == 'not_run'
    assert result.construction.execution['structure_validation'] == 'passed'
    assert result.counts['structures_published'] == 1
    assert result.counts['entity_types'] == 1
    assert result.counts['relation_types'] == 1
    assert result.counts['mappings'] == 2
    assert result.construction.summary['published_structures'] == 1
    publication, = result.construction.execution['structure_publications']
    assert publication['draft_id'] == 'items'
    assert publication['validation_level'] == 'structure'
    assert publication['data_validation'] == 'not_run'
    assert publication['instance_enumeration'] == 'not_run'
    assert (tools.workspace.root / publication['path']).is_file()
    # Exercise typed serialization; nested artifact records must survive API/report output.
    assert type(result).model_validate_json(result.model_dump_json()) == result


def test_structure_plan_does_not_complete_an_instance_goal(environment, monkeypatch):
    config, source, tools, _ = environment
    config = config.model_copy(update={'construction_target': 'instances'})
    monkeypatch.setattr('lightgraph.construction.runner.ConstructionTools', lambda *a, **kw: tools)
    def runtime(*args, **kwargs):
        assert tools.call('validate_graph_draft', draft_id='items', mode='structure')['passed']
        assert tools.call('publish_graph_draft', draft_id='items', mode='structure')['status'] == 'ok'
        return {'finish_reason': 'completed', 'budget': {'calls': 2}}
    result = run_construction(config, adapters=[source], runtime=runtime)
    assert result.status == 'COMPLETED_WITH_GAPS'
    assert result.rules_published == []


def test_structure_permission_changes_invalidate_publication(environment):
    _, _, tools, _ = environment
    tools.call('validate_graph_draft', draft_id='items')
    tools.config.sources[0].allowed_columns = {'public.items': ['code']}
    assert tools.call('publish_graph_draft', draft_id='items')['code'] == 'structure_validation_required'
