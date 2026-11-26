import pytest

from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workflow import load_unit_contributor
from lightgraph.documents import DocumentCorpus, load_documents
from lightgraph.tests.test_publication_identity_authority import (
    _config, _draft, _publish, _read_identity_refs, _separate, _source,
)
from lightgraph.tests.test_scoped_workflow_document_binding import (
    SHARED_ACCESSION_SECTIONS, merge_csv_units_citing_registry,
)


def _units(root, *, west_key='code', local_exception=False, cite=True):
    path = root / 'manual.md'
    override = ('  Exception:\n    scope: {tables: [db.public.west]}\n    identity_contract:\n'
                '      domain: asset_catalog\n      scope: source_local\n'
                '      key_columns: {"db:public.west": [code]}\n') if local_exception else ''
    path.write_text(
        '---\nid: assets\npurpose: data_description\nscope: {tables: [db.public.east, db.public.west]}\n'
        'sections:\n  Catalog:\n    identity_contract:\n      domain: asset_catalog\n      scope: shared\n'
        f'      key_columns: {{"db:public.east": [code], "db:public.west": [{west_key}]}}\n' + override
        + '---\n# Catalog\n设备编号与登记范围见本节声明。\n'
        + ('# Exception\n该表独立维护编号。\n' if local_exception else ''), encoding='utf-8')
    config = _config().model_copy(update={'documents': [str(path)]})
    branches = []
    for table in ('east', 'west'):
        out = root / table
        with _source() as db:
            tools = ConstructionTools(config, out / 'construction', adapters=[db])
            try:
                if local_exception and table == 'east':
                    result = tools.documents.context_for(db.describe()[0], ['code'])
                    assert all(b['table'] == 'db:public.east' for s in result for b in s.get('identity_bindings', []))
                refs = _read_identity_refs(tools, table, {'Catalog'} if cite or table == 'east' else set())
                draft = _draft(table, 'global', refs[0])
                draft['entities'][0]['identity_evidence_refs'] = refs
                _publish(tools, draft)
            finally:
                tools.close()
        branches.append(load_unit_contributor(out, table))
    return branches


def test_authored_contract_replays_only_at_the_cited_key_version(tmp_path):
    branches = _units(tmp_path)
    merged = merge_construction_graphs(branches)
    asset, = [n for n in merged['nodes'] if n['type'] == 'Asset']
    assert {s['provenance']['source']['table'] for s in asset['supports']} == {'east', 'west'}
    assert len(merged['edges']) == 2
    for branch in branches:
        original = next(n for n in branch['graph']['nodes'] if n['type'] == 'Asset')
        marker = original['supports'][0]['provenance']['global_identity_authority']
        assert len(marker['key_sections_v1']) == len(marker.pop('shared_key_sections_v2')) == 1
    _separate(merge_construction_graphs(branches))


@pytest.mark.parametrize('kwargs', [{'west_key': 'name'}, {'local_exception': True}, {'cite': False}])
def test_unbound_uncited_or_source_local_contract_keeps_observations_separate(tmp_path, kwargs):
    _separate(merge_construction_graphs(_units(tmp_path, **kwargs)))


@pytest.mark.parametrize('change', ['exception', 'conditional', 'field_only', 'unrelated_key', 'different_issuer'])
def test_prose_that_does_not_establish_this_key_cannot_authorize_identity(tmp_path, change):
    shared, side_a, side_b = SHARED_ACCESSION_SECTIONS
    modified = {
        'exception': shared + ' There are exceptions for another edition.',
        'conditional': 'Only for the pilot: ' + shared,
        'field_only': 'This field records the specimen accession.',
        'unrelated_key': shared.replace('accession,', 'version,'),
        'different_issuer': shared.replace('Registry R', 'Registry T'),
    }[change]
    graph = merge_csv_units_citing_registry(tmp_path, (modified, side_a, side_b))
    assert len([n for n in graph['nodes'] if n['type'] == 'Specimen']) == 2
    assert len(graph['edges']) == 2


def test_composite_claim_binds_all_ordered_columns_and_only_exposes_visible_keys(tmp_path):
    from lightgraph.contracts import ColumnSpec, SourceRef, TableSpec
    from lightgraph.documents.identity_contract import shared_key_claim_refs
    file = tmp_path / 'business.md'
    file.write_text('---\nid: field-manual\npurpose: data_description\nscope: {tables: [db.public.stock]}\n'
                    'sections:\n  版本范围:\n    identity_contract:\n      domain: 备件目录\n      scope: shared\n'
                    '      key_columns: {"db:public.stock": [sku, edition], "legacy:private.parts": [serial, year]}\n'
                    '---\n# 版本范围\n见已审目录。', encoding='utf-8')
    schema = [ColumnSpec(name=n) for n in ['sku', 'edition']]
    table = TableSpec(source_id='db', name='stock', columns=schema)
    corpus = DocumentCorpus(load_documents([str(file)])).bind_catalog([table])
    scoped, = corpus.context_for(table, ['sku'])
    binding, = scoped['identity_bindings']
    assert binding == {'table': 'db:public.stock', 'domain': '备件目录', 'scope': 'shared',
                       'key_columns': ['sku', 'edition']}
    r = 'versioned-locator'
    groups = [{r: scoped}, {r: scoped}]
    def claims(cols, entries):
        return shared_key_claim_refs('备件目录', ['code', 'version'], cols, table, entries, {r})
    assert claims(['sku', 'edition'], groups) == {r}
    assert not claims(['edition', 'sku'], groups)
    assert not claims(['sku', 'edition'], [groups[0], {}])
    stripped = table.model_copy(update={'columns': schema[:1]})
    restricted, = corpus.context_for(stripped, ['sku'])
    assert restricted['identity_bindings'] == [{'table': 'db:public.stock', 'key_columns': ['sku'], 'incomplete': True}]
    assert not shared_key_claim_refs('备件目录', ['code'], ['sku'], stripped, [{r: restricted}], {r})
    source_only = SourceRef(source_id='db', schema='public', table='stock')
    assert 'identity_bindings' not in corpus.context_for(source_only, ['sku'])[0]


@pytest.mark.parametrize('same_document', [True, False])
def test_uncited_independent_policy_cannot_be_dropped_from_a_prose_claim(tmp_path, same_document):
    from lightgraph.documents.identity_contract import shared_key_claim_refs
    prefix = ('---\nid: registry\npurpose: data_description\n'
              'scope: {tables: [db.public.east], columns: [code]}\n---\n')
    positive = '# Rule\ncode identifies one product across all catalogs.\n'
    restriction = '# Restriction\nEach catalog assigns its own numbers.\n'
    first = tmp_path / 'one.md'
    first.write_text(prefix + positive + (restriction if same_document else ''), encoding='utf-8')
    paths = [first]
    if not same_document:
        other = tmp_path / 'two.md'
        other.write_text(prefix.replace('id: registry', 'id: independent') + restriction, encoding='utf-8')
        paths.append(other)
    with _source() as db:
        table = db.describe()[0]
        rows = DocumentCorpus(load_documents(paths)).bind_catalog([table]).context_for(table, ['code'])
    visible = {s['section_id']: s for s in rows}
    cited = {s['section_id'] for s in rows if s['heading'] == 'Rule'}
    assert len(visible) == 2 and len(cited) == 1
    assert not shared_key_claim_refs('product_directory', ['code'], ['code'], table, [visible], cited)

@pytest.mark.parametrize('other_evidence, grants', [
    ('table_background', True), ('field_restriction', False), ('shared_plus_prose', False),
    ('local_wider', False), ('local_narrower', False),
])
def test_table_notes_and_key_restrictions_have_different_effects_on_a_structured_claim(tmp_path, other_evidence, grants):
    from lightgraph.documents.identity_contract import shared_key_claim_refs
    keys = ['code', 'plant'] if other_evidence == 'local_narrower' else ['code']
    prefix = ('---\npurpose: data_description\nscope: {tables: [db.public.east]}\n')
    pos = tmp_path / 'business1.md'
    pos.write_text(prefix+'sections:\n  Endorsed:\n    identity_contract:\n      scope: shared\n'
                   f'      domain: asset_catalog\n      key_columns: {{"db:public.east": {keys}}}\n'
                   '---\n# Endorsed\n登记规则。', encoding='utf-8')
    neg = tmp_path / 'business2.md'
    independent = '---\n# Local\nEach site assigns its own numbers.'
    if other_evidence == 'table_background':
        independent = ('---\n# 表概况\n各工厂按月提交明细。CSV 文件编码为 UTF-8。\n'
                       '# 字段清单\n| 字段名 | 类型 |\n| --- | --- |\n| code | text |\n| name | text |')
    elif other_evidence == 'field_restriction':
        independent = ('---\n# 表说明\nPhysical column `code` needs `plant` to distinguish historical entries.')
    elif other_evidence in {'local_wider', 'local_narrower'}:
        local_keys = ['code', 'plant'] if other_evidence == 'local_wider' else ['code']
        independent = ('sections:\n  Local:\n    identity_contract:\n      scope: source_local\n'
                       f'      domain: asset_catalog\n      key_columns: {{"db:public.east": {local_keys}}}\n'
                       + independent)
    neg.write_text(prefix+independent, encoding='utf-8')
    with _source() as db:
        table = db.describe()[0]
        corpus = DocumentCorpus(load_documents([pos, neg])).bind_catalog([table])
        groups = [{s['section_id']: s for s in corpus.context_for(table, [col])} for col in keys]
    cited = {ref for ref, s in groups[0].items() if s['heading'] == 'Endorsed'}
    assert len(cited) == 1
    assert shared_key_claim_refs('asset_catalog', keys, keys, table, groups, cited) == (cited if grants else set())


@pytest.mark.parametrize('registry', [
    {'catalog': 'assets', 'version': 'v3', 'key_roles': ['number', 'edition']},
    {'issuer': 'board', 'version': 'v3', 'key_roles': ['number', 'edition']},
    {'issuer': 'board', 'catalog': 'assets', 'key_roles': ['number', 'edition']},
    {'issuer': 'board', 'catalog': 'assets', 'version': ' ', 'key_roles': ['number', 'edition']},
    {'issuer': 'board', 'catalog': 'assets', 'version': 'v3', 'key_roles': ['number']},
    {'issuer': 'board', 'catalog': 'assets', 'version': 'v3', 'key_roles': ['number', 'number']},
])
def test_registry_reference_requires_version_and_an_unambiguous_role_for_every_key_column(registry):
    from pydantic import ValidationError
    from lightgraph.documents.identity_contract import DocumentIdentityContract

    with pytest.raises(ValidationError):
        DocumentIdentityContract(domain='asset-directory', scope='shared',
                                 key_columns={'db:public.assets': ['serial', 'revision']}, registry=registry)
