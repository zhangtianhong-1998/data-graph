"""Build and query a graph whose object types, relations and scopes come from its sources."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from lightgraph.construction.access import filter_graph_scopes
from lightgraph.construction.executor import execute_draft
from lightgraph.construction.models import GraphDraft
from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.sources import MemorySource


def catalogue():
    columns = ['work', 'title', 'cited_work', 'cited_title', 'edition']
    table = TableSpec(source_id='library', name='citations', columns=[ColumnSpec(name=key) for key in columns])
    mapping = {'source_id': table.source_id, 'table': table.name}
    draft = {'draft_id': 'citations',
             'entities': [{'type': '手稿', 'key_names': ['accession'], 'mappings': [
                 {**mapping, 'key_columns': [key], 'label_column': label, 'applicability': {'status': 'global'}}
                 for key, label in [('work', 'title'), ('cited_work', 'cited_title')]]}],
             'relations': [{'type': '参照', 'source_type': '手稿', 'target_type': '手稿',
                            'hierarchy_orientation': 'none', 'mappings': [
                                {**mapping, 'source_key_columns': ['work'], 'target_key_columns': ['cited_work'],
                                 'applicability': {'status': 'scoped', 'columns': {'版本': 'edition'}},
                                 'context_columns': columns}]}]}
    rows = [('A', '山行札记', 'B', '观星序', '初刊'), ('B', '观星序', 'C', '天象录', '初刊'),
            ('B', '观星序', 'D', '校勘记', '再版')]
    return table, draft, rows


def test_arbitrary_relation_and_scopes_round_trip_without_changing_draft_identity():
    table, draft, rows = catalogue()
    legacy = deepcopy(draft)
    for definition in legacy['entities'] + legacy['relations']:
        for mapping in definition['mappings']:
            mapping['business_context'] = mapping.pop('applicability')
    authored = GraphDraft.model_validate(draft)
    assert authored.model_dump(by_alias=True) == GraphDraft.model_validate(legacy).model_dump(by_alias=True)
    with MemorySource([table], {table.qualified_name: rows}) as source:
        result = execute_draft(draft, [source], [table])
    assert result['passed'], result['diagnostics']
    graph = result['graph']
    selected = filter_graph_scopes(graph, ['public'], business_context={'版本': '初刊'})
    labels = {n['id']: n['label'] for n in selected['nodes']}
    assert {(labels[e['source']], e['type'], labels[e['target']]) for e in selected['edges']} == {
        ('山行札记', '参照', '观星序'), ('观星序', '参照', '天象录')}
    assert len(graph['edges']) == 3
    assert all(e['supports'][0]['row_context']['values']['edition'] == '初刊' for e in selected['edges'])


def test_ambiguous_scope_input_rejected_and_omitted_scope_stays_unknown():
    _, draft, _ = catalogue()
    mapping = draft['relations'][0]['mappings'][0]
    mapping['business_context'] = {'status': 'global'}
    with pytest.raises(ValidationError):
        GraphDraft.model_validate(draft)
    for name in ('business_context', 'applicability'):
        del mapping[name]
    assert GraphDraft.model_validate(draft).relations[0].mappings[0].business_context.status == 'unknown'
