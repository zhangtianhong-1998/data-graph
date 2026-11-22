"""Compare record and relationship projections which carry extra descriptive cells."""
from collections import Counter

import pytest

from lightgraph.construction.executor import execute_draft
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.sources import MemorySource
from lightgraph.tests.test_source_hierarchy_replays import code, contributor


def _catalog(unit, *, verbose=False, missing_keys=False, unbound=False, rename=False, region='A', ordinal=False):
    columns = ['shelf', 'caption', 'translation', 'item', 'title', 'alternate', 'collection']
    table = TableSpec(source_id='archive', name='catalog', columns=[ColumnSpec(name=c) for c in columns])
    rows = [('A1', 'Classics', 'Classiques', 'N2', 'Letters', 'Lettres', region),
            ('A1', 'Classics', 'Classiques', 'N3', 'Poems', 'Vers' if rename else 'Poèmes', region)]
    context = ['collection']
    if not missing_keys:
        context.extend(['shelf', 'item'])
    if verbose:
        context.extend(['caption', 'translation', 'title', 'alternate'])
    common = {'source_id': 'archive', 'table': 'catalog', 'context_columns': context}
    mappings = [{**common, 'key_columns': [key], 'label_column': label,
                 **({} if unbound else {'english_name_column': second})}
                for key, label, second in [('shelf', 'caption', 'translation'), ('item', 'title', 'alternate')]]
    if verbose:
        for mapping in mappings:
            mapping['properties'] = {'held_in': 'collection'}
    if ordinal:
        for mapping, location in zip(mappings, ('excel_column_1', 'excel_column_4')):
            mapping['constant_properties'] = {'source_position': location}
    kind = f'Item{unit}'
    draft = {'draft_id': f'catalog{unit}', 'entities': [{'type': kind, 'identity_scope': 'global',
             'identity_domain': f'codes{unit}', 'identity_evidence_refs': [f'e{unit}'], 'key_names': ['key'],
             'mappings': mappings}], 'relations': [{'type': f'holds{unit}', 'source_type': kind, 'target_type': kind,
             'hierarchy_orientation': 'parent_to_child',
             'mappings': [{**common, 'source_key_columns': ['shelf'], 'target_key_columns': ['item']}]}]}
    with MemorySource([table], {table.qualified_name: rows}) as adapter:
        result = execute_draft(draft, [adapter], [table])
    assert result['passed'], result['diagnostics']
    return contributor(f'unit{unit}', dict(result['graph'], draft_id=draft['draft_id'], draft_revision=1))


def test_extra_captions_of_witnessed_row_keys_do_not_split_the_catalog():
    units = [_catalog(1), _catalog(2, verbose=True)]
    baseline = merge_construction_graphs(units, recognize_source_replays=False)
    graph = merge_construction_graphs(units)
    assert Counter(map(code, graph['nodes'])) == {'A1': 1, 'N2': 1, 'N3': 1}
    assert len(graph['edges']) == len(baseline['edges']) == 4
    for kind in ('nodes', 'edges'):
        def verbose_rows(g):
            return Counter((s['row_context']['id'], s['row_context']['values']['translation'],
                            s['row_context']['values']['alternate'], s['row_context']['values']['collection'])
                           for item in g[kind] for s in item['supports'] if 'alternate' in s['row_context']['values'])
        assert verbose_rows(graph) == verbose_rows(baseline)
    diagnostics = graph['fusion_diagnostics']['hierarchy_replay_diagnostics']
    assert diagnostics['equal_node_pairs_by_result'] == {'passed_before_endpoint_remap': 3}


@pytest.mark.parametrize('change', [{'missing_keys': True}, {'unbound': True}, {'region': 'B'}, {'rename': True}])
def test_caption_normalization_requires_the_key_role_scope_and_consistent_observed_name(change):
    graph = merge_construction_graphs([_catalog(1), _catalog(2, verbose=True, **change)])
    assert Counter(map(code, graph['nodes']))['N3'] == 2


def test_five_views_keep_only_the_unsupported_ordinal_separate():
    units = [_catalog(1), _catalog(2), _catalog(3), _catalog(4, verbose=True), _catalog(5, ordinal=True)]
    graph = merge_construction_graphs(units)
    assert Counter(map(code, graph['nodes'])) == {'A1': 2, 'N2': 2, 'N3': 2}
    assert len(graph['edges']) == 10
    assert graph['fusion_diagnostics']['hierarchy_replay_diagnostics']['physical_set_outcomes'] == [
        {'input_nodes': 5, 'output_nodes': 2, 'sets': 3}]
