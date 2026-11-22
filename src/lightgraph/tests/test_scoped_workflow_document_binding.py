import pytest

from lightgraph.construction.access import access_fingerprint
from lightgraph.construction.tools import ConstructionTools, _drop_unbound_documents
from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.documents import DocumentCorpus, load_documents
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource


def _documents(tmp_path, tables):
    paths = []
    for index, table in enumerate(tables):
        path = tmp_path / f'document{index}.md'
        path.write_text(f'---\nid: doc{index}\npurpose: data_description\nscope:\n  tables: [{table}]\n---\n# Fields\nField description.\n')
        paths.append(str(path))
    return paths


@pytest.mark.parametrize('bound_tables, expected', [
    (['income', 'ebg_order'], ['doc0']), (['income'], ['doc0']), ([], []),
])
def test_scoped_tools_only_keep_bindable_documents(tmp_path, bound_tables, expected):
    config = RunConfig(sources=[], documents=_documents(tmp_path, bound_tables))
    table = TableSpec(source_id='ict', name='income', columns=[ColumnSpec(name='code')])
    fingerprint = access_fingerprint(config)
    with MemorySource([table], {}) as adapter:
        tools = ConstructionTools(config, tmp_path / 'construction', adapters=[adapter], drop_unbound_documents=True)
        try:
            assert [d.document_id for d in tools.documents.documents] == expected
            assert access_fingerprint(tools.config) == fingerprint
        finally:
            tools.close()


def test_project_tools_keep_strict_document_binding(tmp_path):
    config = RunConfig(sources=[], documents=_documents(tmp_path, ['income', 'ebg_order']))
    table = TableSpec(source_id='ict', name='income', columns=[ColumnSpec(name='code')])
    with MemorySource([table], {}) as adapter:
        with pytest.raises(LightGraphError, match='invalid_document_table_binding'):
            ConstructionTools(config, tmp_path / 'construction', adapters=[adapter])


def test_scoped_binding_does_not_hide_ambiguity_after_missing_binding(tmp_path):
    corpus = DocumentCorpus(load_documents(_documents(tmp_path, ['missing', 'income'])))
    tables = [TableSpec(source_id=sid, name='income', columns=[ColumnSpec(name='code')]) for sid in ['a', 'b']]
    with pytest.raises(LightGraphError, match='ambiguous_document_table_binding'):
        _drop_unbound_documents(corpus, tables)


@pytest.mark.parametrize('scope', [{'source_ids': ['missing']}, {'tables': ['income'], 'columns': ['missing']}])
def test_scoped_binding_drops_missing_source_or_column(tmp_path, scope):
    corpus = DocumentCorpus(load_documents(_documents(tmp_path, ['income'])))
    from lightgraph.documents.core import DocumentScope
    corpus.documents[0].scope = DocumentScope(**scope)
    table = TableSpec(source_id='ict', name='income', columns=[ColumnSpec(name='code')])
    assert _drop_unbound_documents(corpus, [table]).documents == []


def test_partial_catalog_does_not_resolve_ambiguous_legacy_names(tmp_path):
    filename = _documents(tmp_path, ['readings, b.public.readings'])
    table = TableSpec(source_id='a', name='readings', columns=[ColumnSpec(name='code')])
    assert _drop_unbound_documents(DocumentCorpus(load_documents(filename)), [table]).documents == []


def test_document_only_identity_requires_issued_sections_covering_the_key(tmp_path):
    from lightgraph.construction.workspace import dump_yaml
    csv = tmp_path / 'observations.csv'
    csv.write_text('code,edition,title\n7,22,Specimen\n', encoding='utf-8')
    path = tmp_path / 'guide.md'
    path.write_text('---\nid: guide\npurpose: data_description\n'
                    'sections:\n  Codes: {scope: {columns: [code]}}\n'
                    '  Editions: {scope: {columns: [edition]}}\n---\n'
                    '# Codes\nThe Registry keys each specimen by its code and edition.\n'
                    '# Editions\nThe edition column is the Registry edition.\n', encoding='utf-8')
    config = RunConfig(sources=[{'id': 'a', 'kind': 'csv_file', 'path': str(csv), 'encoding': 'utf-8',
        'delimiter': ',', 'header': True, 'null_values': [''], 'type_inference': 'text',
        'table_name': 'readings'}], documents=[str(path)], model={'mode': 'in_environment'})
    tools = ConstructionTools(config, tmp_path / 'construction')
    try:
        sections = [tools.call('read_material', name='document:guide', page=i) for i in range(2)]
        refs = [section['evidence_refs'][0] for section in sections]
        version = f"guide@{sections[0]['section']['revision']}"
        entity = {'type': 'Specimen', 'key_names': ['code', 'edition'], 'identity_scope': 'global',
                  'identity_domain': 'Registry', 'mappings': [{'source_id': 'a', 'table': 'readings',
                       'key_columns': ['code', 'edition'], 'label_column': 'title'}]}
        for evidence, missing in [([version], ['code', 'edition']), ([refs[0]], ['edition'])]:
            draft = {'draft_id': 'objects', 'entities': [{**entity, 'identity_evidence_refs': evidence}]}
            rejected = tools.call('save_graph_draft', yaml_text=dump_yaml(draft))
            assert rejected['code'] == 'identity_document_not_applicable'
            assert rejected['diagnostics'][0]['columns'] == missing
        draft['entities'][0]['identity_evidence_refs'] = refs
        assert tools.call('save_graph_draft', yaml_text=dump_yaml(draft))['status'] == 'ok'
    finally:
        tools.close()


def test_shared_field_dictionary_in_two_units_without_database_constraints(tmp_path):
    from lightgraph.construction.merger import merge_construction_graphs
    from lightgraph.construction.workflow import build_unit_config, load_unit_contributor
    from lightgraph.construction.workspace import dump_yaml
    from lightgraph.runtime.config import WorkflowUnitConfig

    doc = tmp_path / 'registry.md'
    doc.write_text(
        '---\nid: registry\npurpose: data_description\n'
        'scope: {tables: [a.public.readings, b.public.readings]}\n'
        'sections:\n  Side A: {scope: {tables: [a.public.readings]}}\n'
        '  Side B: {scope: {tables: [b.public.readings]}}\n---\n'
        '# Shared\nRegistry R assigns each specimen exactly one accession, unique across its sites.\n'
        '# Side A\nThe accession_a column holds the full Registry R specimen accession.\n'
        '# Side B\nThe accession_b column holds the full Registry R specimen accession.\n',
        encoding='utf-8')
    sources = []
    for sid in ['a', 'b']:
        csv = tmp_path / (sid + '.csv')
        csv.write_text(f'accession_{sid},label,local_class\n0007,Specimen 7,02\n', encoding='utf-8')
        sources.append({'id': sid, 'kind': 'csv_file', 'path': str(csv), 'encoding': 'utf-8',
                        'delimiter': ',', 'header': True, 'null_values': [''], 'type_inference': 'text',
                        'table_name': 'readings'})
    config = RunConfig(sources=sources, documents=[str(doc)], model={'mode': 'in_environment'})
    units = []
    for sid in ['a', 'b']:
        out = tmp_path / sid
        unit_config = build_unit_config(config, WorkflowUnitConfig(unit_id=sid, source_ids=[sid]),
                                        unit_output_dir=out)
        tools = ConstructionTools(unit_config, out / 'construction', drop_unbound_documents=True)
        try:
            catalog = tools.call('describe_sources')
            assert [doc['id'] for doc in catalog['documents']] == ['registry']
            table, = catalog['tables']
            assert not table['declared_unique_keys'] and not table['declared_foreign_keys']
            page = tools.call('read_material', name='document:registry')
            assert page['section']['heading'] == 'Shared'
            local = tools.call('read_material', name='document:registry', page=page['next_page'])
            assert local['section']['heading'] == 'Side ' + sid.upper() and local['next_page'] is None
            assert tools.call('read_material', name='document:registry', page=2)['code'] == 'unknown_document_page'
            search = tools.call('inspect_source', table=table['table'], operation='search_knowledge')
            assert {section['heading'] for section in search['data']['sections']} == {'Shared', 'Side ' + sid.upper()}
            refs = [*page['evidence_refs'], *local['evidence_refs']]
            for ref in refs:
                assert tools.workspace.read_evidence(ref).details['sources'] == [
                    {'source_id': sid, 'schema': 'public', 'table': 'readings'}]
            source = {'source_id': sid, 'table': 'readings'}
            draft = {'draft_id': 'readings', 'entities': [
                {'type': 'Specimen', 'key_names': ['accession'], 'identity_scope': 'global',
                 'identity_domain': 'Registry R', 'identity_evidence_refs': refs,
                 'mappings': [{**source, 'key_columns': ['accession_' + sid], 'label_column': 'label'}]},
                {'type': 'LocalClass', 'key_names': ['code'],
                 'mappings': [{**source, 'key_columns': ['local_class'], 'label_column': 'local_class'}]},
            ], 'relations': [{'type': 'classified_as', 'source_type': 'Specimen', 'target_type': 'LocalClass',
                'mappings': [{**source, 'source_key_columns': ['accession_' + sid],
                              'target_key_columns': ['local_class']}]}]}
            assert tools.call('save_graph_draft', yaml_text=dump_yaml(draft))['status'] == 'ok'
            assert tools.call('validate_graph_draft', draft_id='readings')['passed']
            assert tools.call('publish_graph_draft', draft_id='readings')['nodes'] == 2
        finally:
            tools.close()
        units.append(load_unit_contributor(out, sid))
    graph = merge_construction_graphs(units, target_access_fingerprint=access_fingerprint(config))
    specimen, = [node for node in graph['nodes'] if node['type'] == 'Specimen']
    assert specimen['identity'] == {'accession': '0007'}
    assert {s['provenance']['source']['source_id'] for s in specimen['supports']} == {'a', 'b'}
    classes = {node['id'] for node in graph['nodes'] if node['type'] == 'LocalClass'}
    assert len(classes) == 2
    assert {(e['source'], e['target']) for e in graph['edges']} == {(specimen['id'], c) for c in classes}
