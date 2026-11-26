import pytest

from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import dump_yaml
from lightgraph.runtime.config import RunConfig


@pytest.fixture
def draft_tools(tmp_path):
    csv = tmp_path / 'readings.csv'
    csv.write_text('code,extra_code,title\n7,17,Seven\n', encoding='utf-8')
    source = {'id': 'samples', 'kind': 'csv_file', 'path': str(csv), 'table_name': 'readings',
              'encoding': 'utf-8', 'delimiter': ',', 'header': True, 'null_values': [''], 'type_inference': 'text'}
    docs = []
    for name, purpose, body in [
        ('guide', 'data_description', '# Other\n' + 'Extra ' * 2800 + '\n# Key\n字段 `code` 为样本编号。'),
        ('notes', 'analysis_knowledge', '# Study\nUse code to chart the results.'),
        ('other', 'data_description', '# Unrelated\nThe imported_code records the legacy value.'),
    ]:
        path = tmp_path / (name + '.md')
        path.write_text(f'---\nid: {name}\npurpose: {purpose}\nscope: {{tables: [samples.public.readings]}}\n'
                        f'---\n{body}\n', encoding='utf-8')
        docs.append(str(path))
    labels = tmp_path / 'labels.md'
    labels.write_text('---\nid: labels\npurpose: data_description\nscope: {columns: [title]}\n---\n'
                      '# Labels\nDisplay code and extra_code alongside the title.\n', encoding='utf-8')
    docs.append(str(labels))
    config = RunConfig(sources=[source], documents=docs, model={'mode': 'in_environment'})
    tools = ConstructionTools(config, tmp_path / 'build')
    try:
        probe = tools.call('inspect_source', table='samples:public.readings', operation='get_metadata')
        yield tools, probe['evidence_ref']
    finally:
        tools.close()


def _draft(refs, *, column='code', scope='global'):
    return {'draft_id': 'objects', 'entities': [{'type': 'Sample', 'key_names': ['id'],
            'identity_scope': scope, 'identity_domain': 'Registry', 'identity_evidence_refs': refs,
            'mappings': [{'source_id': 'samples', 'table': 'readings', 'key_columns': [column],
                          'label_column': 'title'}]}]}


def test_save_points_to_authorized_field_material_without_issuing_or_citing_it(draft_tools):
    tools, probe = draft_tools
    before = tools.workspace._manifest()['evidence']
    saved = tools.call('save_graph_draft', yaml_text=dump_yaml(_draft([probe])))
    field, = saved['identity_evidence_hints']['fields']
    assert (field['entity_type'], field['mapping_index'], field['table'], field['key_column']) == (
        'Sample', 0, 'samples:public.readings', 'code')
    location, = field['sections']
    assert location['read'] == {'tool': 'read_material', 'name': 'document:guide', 'page': 2}
    assert before == tools.workspace._manifest()['evidence']
    assert tools.workspace.get_draft('objects').draft.entities[0].identity_evidence_refs == [probe]
    read = dict(location['read'])
    page = tools.call(read.pop('tool'), **read)
    assert page['section']['section_id'] == location['section_id']
    assert page['section']['heading'] == 'Key'
    revised = tools.call('save_graph_draft', yaml_text=dump_yaml(_draft([probe, *page['evidence_refs']])))
    assert revised['status'] == 'ok' and 'identity_evidence_hints' not in revised


@pytest.mark.parametrize('scope,column', [('unknown', 'code'), ('global', 'extra_code')])
def test_save_silent_for_local_identity_or_unmentioned_key(draft_tools, scope, column):
    tools, probe = draft_tools
    result = tools.call('save_graph_draft', yaml_text=dump_yaml(_draft([probe], column=column, scope=scope)))
    assert result['status'] == 'ok'
    assert 'identity_evidence_hints' not in result


def test_undeclared_scope_points_to_the_authored_full_key_without_inheriting_another_types_domain(draft_tools, tmp_path):
    original, _ = draft_tools
    doc = tmp_path / 'registered.md'
    contracts = {'Registry': {'identity_contract': {'domain': 'Registry', 'scope': 'shared',
                 'key_columns': {'samples:public.readings': ['code']},
                 'registry': {'issuer': 'Office', 'catalog': 'Samples', 'version': 'v4', 'key_roles': ['sample']}}}}
    doc.write_text('---\n' + dump_yaml({'id': 'registered', 'purpose': 'data_description',
                   'scope': {'tables': ['samples.public.readings'], 'columns': ['code']}, 'sections': contracts})
                   + '---\n# Registry\nThe samples register uses this key.\n', encoding='utf-8')
    config = original.config.model_copy(update={'documents': [str(doc)]})
    tools = ConstructionTools(config, tmp_path / 'new')
    try:
        draft = _draft([], scope='unknown')
        draft['entities'][0].pop('identity_domain')
        saved = tools.call('save_graph_draft', yaml_text=dump_yaml(draft))
        entry, = saved['identity_evidence_hints']['fields']
        assert entry['reason'] == 'undeclared_scope_with_authored_key' and entry['key_columns'] == ['code']
        current = tools.workspace.get_draft('objects').draft.entities[0]
        assert current.identity_scope == 'unknown' and not current.identity_domain and not current.identity_evidence_refs
        command = dict(entry['sections'][0]['read'])
        result = tools.call(command.pop('tool'), **command)
        draft['entities'][0].update(identity_scope='global', identity_domain='Registry',
                                    identity_evidence_refs=result['evidence_refs'])
        revised = tools.call('save_graph_draft', yaml_text=dump_yaml(draft))
        assert revised['status'] == 'ok' and 'identity_evidence_hints' not in revised
    finally:
        tools.close()
