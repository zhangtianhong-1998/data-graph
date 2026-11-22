"""Construction records only complete authorized catalog key declarations on row observations."""
import pytest

from lightgraph.construction.executor import execute_draft
from lightgraph.contracts import ColumnSpec, ExtractionGoal, ForeignKeySpec, TableSpec
from lightgraph.sources import MemorySource


def _tables():
    master = TableSpec(source_id='archive', name='names',
                       columns=[ColumnSpec(name=c, primary_key=True) for c in ('series', 'accession')],
                       unique_keys=[['series', 'accession']])
    entries = TableSpec(source_id='archive', name='entries',
                        columns=[ColumnSpec(name=c) for c in ('c1', 'c2')],
                        foreign_keys=[ForeignKeySpec(name='entry_reference', columns=['c1', 'c2'],
                            target_schema='public', target_table='names', target_columns=['series', 'accession'])])
    return entries, master


def _project(tables, table, keys, *, goal=None):
    rows = {t.qualified_name: [('01', 'A')[:len(t.columns)]] for t in tables}
    draft = {'draft_id': 'index_collection', 'entities': [{'type': 'Record',
        'key_names': keys, 'mappings': [{'source_id': table.source_id, 'schema': table.schema_name,
                                        'table': table.name, 'key_columns': keys}]}]}
    with MemorySource(tables, rows) as source:
        result = execute_draft(draft, [source], tables, goal=goal)
    assert result['passed'], result['diagnostics']
    return result['graph']['nodes'][0]['supports'][0]['provenance']


def test_projection_records_the_same_ordered_dictionary_address_for_both_sides():
    entries, master = _tables()
    tables = [entries, master]
    witness = _project(tables, entries, ['c1', 'c2'])['catalog_identity_reference']
    target = {'source_id': 'archive', 'schema': 'public', 'table': 'names', 'columns': ['series', 'accession']}
    assert witness == {'kind': 'foreign_key', 'local_columns': ['c1', 'c2'], 'target': target}
    assert _project(tables, master, ['series', 'accession'])['catalog_identity_reference'] == {
        'kind': 'unique_key', 'local_columns': ['series', 'accession'], 'target': target}


@pytest.mark.parametrize('reason', ['target_table_hidden', 'target_column_hidden', 'reversed', 'partial',
                                    'target_not_unique', 'ambiguous_foreign_keys', 'excluded', 'evidence_only'])
def test_missing_or_ambiguous_key_declaration_cannot_issue_a_reference(reason):
    entries, master = _tables()
    tables = [entries, master]
    keys = ['c1', 'c2']
    goal = None
    if reason == 'target_table_hidden':
        tables = [entries]
    elif reason == 'target_column_hidden':
        master.columns = master.columns[:1]
    elif reason == 'reversed':
        keys.reverse()
    elif reason == 'partial':
        keys.pop()
    elif reason == 'target_not_unique':
        master.unique_keys = []
    elif reason == 'ambiguous_foreign_keys':
        other = master.model_copy(update={'name': 'other_names'})
        tables.append(other)
        entries.foreign_keys.append(entries.foreign_keys[0].model_copy(update={'target_table': other.name}))
    elif reason == 'excluded':
        goal = ExtractionGoal(source_scope=['entries'])
    else:
        goal = ExtractionGoal(evidence_only_columns=['accession'])
    assert 'catalog_identity_reference' not in _project(tables, entries, keys, goal=goal)


def test_one_to_one_foreign_key_does_not_reinterpret_a_tables_own_identity():
    entries, master = _tables()
    entries.unique_keys = [['c1', 'c2']]
    witness = _project([entries, master], entries, ['c1', 'c2'])['catalog_identity_reference']
    assert witness['kind'] == 'unique_key'
    assert witness['target']['table'] == entries.name
