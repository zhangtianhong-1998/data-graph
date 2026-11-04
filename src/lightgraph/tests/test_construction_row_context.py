"""Row contexts retain observed paths without splitting business identities."""
from copy import deepcopy

import pytest
from lightgraph.contracts import ColumnSpec, TableSpec, ExtractionGoal
from lightgraph.sources import MemorySource
from lightgraph.construction.models import GraphDraft
from lightgraph.construction.executor import execute_draft
from lightgraph.construction.access import filter_graph_scopes


def example():
    columns = ['tenant', 'a', 'b', 'c']
    table = TableSpec(source_id='s', name='paths', columns=[ColumnSpec(name=c) for c in columns])
    shared = {'source_id': 's', 'table': 'paths', 'context_columns': columns}
    draft = GraphDraft.model_validate({'draft_id': 'paths', 'entities': [{'type': 'Item',
        'key_names': ['code'], 'mappings': [dict(shared, key_columns=[col],
            constant_properties={'position': i}) for i, col in enumerate(columns[1:], 1)]}],
        'relations': [{'type': 'contains', 'source_type': 'Item', 'target_type': 'Item',
            'acyclic': True, 'mappings': [dict(shared, source_key_columns=[left], target_key_columns=[right],
                constant_properties={'position_pair': pair}, equal_endpoints='omit')
                for left, right, pair in [('a', 'b', '1-2'), ('b', 'c', '2-3')]]}]})
    return table, draft


def test_context_pairs_survive_edge_dedup_and_padding_with_scope_filter():
    table, draft = example()
    rows = [('public', 'A', 'B', 'C'), ('public', 'X', 'A', 'B'),
            ('public', 'C', 'C', 'C'), ('private', 'SECRET', 'A', 'B')]
    with MemorySource([table], {table.qualified_name: rows}) as source:
        result = execute_draft(draft, [source], [table], scope_bindings=[
            {'source_id': 's', 'table': 'paths', 'columns': ['tenant']}])
    assert result['passed'], result['diagnostics']
    graph = filter_graph_scopes(result['graph'], ['public'])
    by_code = {n['identity']['code']: n for n in graph['nodes']}
    assert len(by_code) == 4 and 'SECRET' not in str(graph)
    a = by_code['A']
    assert a['properties']['position'] == [1, 2]
    edge = next(e for e in graph['edges'] if e['source'] == a['id'] and e['target'] == by_code['B']['id'])
    assert edge['properties']['position_pair'] == ['1-2', '2-3']
    assert {tuple(s['row_context']['values'][c] for c in ['a', 'b', 'c']) for s in edge['supports']} == {
        ('A', 'B', 'C'), ('X', 'A', 'B')}
    # Every occurrence in the all-equal row remains supported although its
    # self edges were omitted. No new leaf/placeholder interpretation is made.
    padding = [s for s in by_code['C']['supports'] if s['row_context']['values']['a'] == 'C']
    assert len(padding) == 3 and len({s['row_context']['id'] for s in padding}) == 1
    assert not any(e['source'] == e['target'] for e in graph['edges'])
    context_ids = {s['row_context']['id'] for n in graph['nodes'] for s in n['supports']}
    assert len(context_ids) == 3
    from lightgraph.construction.statistics import graph_statistics
    stats = graph_statistics(graph)
    assert stats['source_row_contexts'] == 3
    assert stats['relation_types']['contains']['nodes_with_incoming_and_outgoing'] == 2
    assert stats['relation_types']['contains']['nodes_without_outgoing_edges'] == 1
    assert stats['relation_types']['contains']['isolated_nodes'] == 0
    # Optional evidence enrichment cannot change business identities or edges.
    plain = draft.model_dump(mode='python', by_alias=True)
    for definition in plain['entities'] + plain['relations']:
        for mapping in definition['mappings']:
            mapping['context_columns'] = []; mapping['constant_properties'] = {}
    with MemorySource([table], {table.qualified_name: rows}) as source:
        baseline = execute_draft(plain, [source], [table])['graph']
    assert {n['id'] for n in baseline['nodes']} == {n['id'] for n in result['graph']['nodes']}
    assert {e['id'] for e in baseline['edges']} == {e['id'] for e in result['graph']['edges']}


def test_context_columns_do_not_bypass_source_or_goal_authorization():
    table, draft = example()
    with MemorySource([table], {table.qualified_name: [('S', 'A', 'B', 'C')]}) as source:
        result = execute_draft(draft, [source], [table], goal=ExtractionGoal(excluded_columns=['tenant']))
        assert not result['passed'] and not result['graph']['nodes']
        assert source.stats.queries == 0


def test_mapping_constant_cannot_shadow_a_source_property():
    _, draft = example()
    value = draft.model_dump(mode='python', by_alias=True)
    value['entities'][0]['mappings'][0]['properties'] = {'position': 'a'}
    with pytest.raises(ValueError, match='property_source_conflict'):
        GraphDraft.model_validate(value)
