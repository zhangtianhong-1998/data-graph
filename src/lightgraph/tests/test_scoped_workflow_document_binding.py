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
