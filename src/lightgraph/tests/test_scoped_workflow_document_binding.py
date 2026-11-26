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


SHARED_ACCESSION_SECTIONS = (
    'Registry R assigns each specimen exactly one accession, unique across its sites.',
    'The accession_a column holds the full Registry R specimen accession.',
    'The accession_b column holds the full Registry R specimen accession.',
)


def merge_csv_units_citing_registry(tmp_path, sections=SHARED_ACCESSION_SECTIONS, *, allow_local_fallback=False):
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
        f'# Shared\n{sections[0]}\n# Side A\n{sections[1]}\n# Side B\n{sections[2]}\n',
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
            saved = tools.call('save_graph_draft', yaml_text=dump_yaml(draft))
            checked = (tools.call('validate_graph_draft', draft_id=draft['draft_id'])
                       if saved['status'] == 'ok' else saved)
            if not checked.get('passed') and allow_local_fallback:
                # A validator may reject the incorrect shared-domain draft before merge.
                codes = [checked.get('code') or '', *(d.get('code', '') for d in checked.get('diagnostics', []))]
                assert any('identity' in code for code in codes), checked
                draft['draft_id'] = 'site_local_readings'
                entity = draft['entities'][0]
                entity.update(identity_scope='unknown')
                del entity['identity_domain'], entity['identity_evidence_refs']
                assert tools.call('save_graph_draft', yaml_text=dump_yaml(draft))['status'] == 'ok'
                checked = tools.call('validate_graph_draft', draft_id=draft['draft_id'])
            assert checked.get('passed'), checked
            assert tools.call('publish_graph_draft', draft_id=draft['draft_id'])['nodes'] == 2
        finally:
            tools.close()
        units.append(load_unit_contributor(out, sid))
    return merge_construction_graphs(units, target_access_fingerprint=access_fingerprint(config))


def test_shared_field_dictionary_in_two_units_without_database_constraints(tmp_path):
    graph = merge_csv_units_citing_registry(tmp_path)
    specimen, = [node for node in graph['nodes'] if node['type'] == 'Specimen']
    assert specimen['identity'] == {'accession': '0007'}
    assert {s['provenance']['source']['source_id'] for s in specimen['supports']} == {'a', 'b'}
    classes = {node['id'] for node in graph['nodes'] if node['type'] == 'LocalClass'}
    assert len(classes) == 2
    assert {(e['source'], e['target']) for e in graph['edges']} == {(specimen['id'], c) for c in classes}



def _csv_source(tmp_path, sid, table):
    path = tmp_path / (sid + '.csv')
    path.write_text('id,title\n1,One\n')
    return {'id': sid, 'kind': 'csv_file', 'path': str(path), 'table_name': table,
            'encoding': 'utf-8', 'delimiter': ',', 'header': True, 'null_values': [''], 'type_inference': 'text'}


@pytest.mark.parametrize('selector', ['items', 'public.items'])
def test_split_rejects_short_document_name_shared_by_siblings(tmp_path, selector):
    from lightgraph.construction.workflow import build_unit_config
    from lightgraph.runtime.config import WorkflowUnitConfig
    config = RunConfig(sources=[_csv_source(tmp_path, sid, 'items') for sid in ('erp', 'crm')],
                       documents=_documents(tmp_path, [selector]))
    for sid in ('erp', 'crm'):
        with pytest.raises(LightGraphError, match='ambiguous_document_table_binding'):
            build_unit_config(config, WorkflowUnitConfig(unit_id=sid, source_ids=[sid]), unit_output_dir=tmp_path / sid)


def test_unique_short_names_in_mixed_document_survive_the_split(tmp_path):
    from lightgraph.construction.workflow import build_unit_config
    from lightgraph.runtime.config import WorkflowUnitConfig
    from lightgraph.web.execution import _resolved_payload

    guide = tmp_path / 'guide.md'
    guide.write_text('---\nid: guide\npurpose: data_description\n'
        'scope: {tables: [items, crm.public.accounts]}\n'
        'sections:\n  Price: {scope: {tables: [items]}}\n  Balance: {scope: {tables: [accounts]}}\n'
        '---\n# Glossary\nCommon terms.\n# Price\nERP only.\n# Balance\nCRM only.\n')
    config = RunConfig(sources=[_csv_source(tmp_path, 'erp', 'items'), _csv_source(tmp_path, 'crm', 'accounts')],
                       documents=[str(guide)])
    for sid, table, heading in [('erp', 'items', 'Price'), ('crm', 'accounts', 'Balance')]:
        out = tmp_path / sid
        unit = build_unit_config(config, WorkflowUnitConfig(unit_id=sid, source_ids=[sid]), unit_output_dir=out)
        payload = _resolved_payload(unit, tmp_path)
        assert payload['document_table_bindings'] and 'document_table_bindings' not in payload['config']
        tools = ConstructionTools(unit, out, drop_unbound_documents=True)
        try:
            pages = [tools.call('read_material', name='document:guide', page=i)['section'] for i in (0, 1)]
            assert [p['heading'] for p in pages] == ['Glossary', heading]
            assert all(p['sources'] == [{'source_id': sid, 'schema': 'public', 'table': table}] for p in pages)
            assert tools.call('read_material', name='document:guide', page=2)['code'] == 'unknown_document_page'
        finally:
            tools.close()


def test_qualified_and_global_documents_do_not_open_unrelated_sources(tmp_path):
    from lightgraph.construction.workflow import build_unit_config
    from lightgraph.runtime.config import WorkflowUnitConfig
    docs = _documents(tmp_path, ['erp.public.items'])
    guide = tmp_path / 'global.md'
    guide.write_text('# Notes\nCross-project glossary.')
    sources = [_csv_source(tmp_path, 'erp', 'items'),
               {'id': 'sibling', 'kind': 'excel_workbook', 'path': str(tmp_path / 'unavailable.xlsx')},
               {'id': 'db', 'kind': 'gauss_database'}]
    config = RunConfig(sources=sources, documents=docs + [str(guide)])
    unit = build_unit_config(config, WorkflowUnitConfig(unit_id='main', source_ids=['erp']), unit_output_dir=tmp_path)
    assert [source.id for source in unit.sources] == ['erp']
    assert unit.documents == config.documents


@pytest.mark.parametrize('version', [1, 2])
@pytest.mark.parametrize('short_names', [False, True])
def test_checkpoint_proof_tracks_resolved_document_tables_only_in_relevant_units(
        tmp_path, monkeypatch, version, short_names):
    from lightgraph.documents import core
    from lightgraph.web.execution import _WorkflowInputProofs, _input_manifest, _workflow_node_inputs

    sources = [_csv_source(tmp_path, sid, name) for sid, name in
               [('erp', 'items'), ('crm', 'accounts'), ('other', 'orders')]]
    items, accounts = ('items', 'accounts') if short_names else ('erp.public.items', 'crm.public.accounts')
    guide = tmp_path / 'guide.md'
    guide.write_text('---\nid: guide\npurpose: data_description\n'
        f'scope: {{tables: [{items}, crm.public.accounts]}}\n'
        f'sections:\n  Price: {{scope: {{tables: [{items}]}}}}\n  Balance: {{scope: {{tables: [{accounts}]}}}}\n'
        '---\n# Glossary\nCommon terms.\n# Price\nERP only.\n# Balance\nCRM only.\n')
    config = RunConfig(sources=sources, documents=[str(guide)], workflow={
        'enabled': True, 'units': [{'unit_id': s['id'], 'source_ids': [s['id']]} for s in sources]})
    files = _input_manifest(config, tmp_path)
    with monkeypatch.context() as legacy:
        legacy.setattr(core, 'resolve_workflow_table_selectors', lambda *args: {})
        previous = _workflow_node_inputs(config, tmp_path, files, version=version)
    current = _workflow_node_inputs(config, tmp_path, files, version=version)
    assert {uid for uid in previous if previous[uid] != current[uid]} == ({'crm', 'erp'} if short_names else set())
    proofs = _WorkflowInputProofs(config, tmp_path, files)
    for uid, old in previous.items():
        if short_names and uid in ('erp', 'crm'):
            with pytest.raises(LightGraphError, match='workflow_resume_input_changed'):
                proofs.match(uid, old, {'input_digest': old}, [None, str(tmp_path)])
        else:
            assert proofs.match(uid, old, {'input_digest': old}, [None]) == (str(tmp_path) if version == 1 else None)
