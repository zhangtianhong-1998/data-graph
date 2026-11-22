"""Repeated source projections can legitimately choose different primary names."""
from copy import deepcopy

import pytest

from lightgraph.construction.executor import execute_draft
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.sources import MemorySource


def _unit(cid, primary, aliases, *, source_id='file', project='M', parent='parent', different_values=False):
    columns = ['code', 'first', 'second', 'third', 'parent', 'other_parent']
    table = TableSpec(source_id=source_id, name='sheet', columns=[ColumnSpec(name=name) for name in columns])
    rows = {table.qualified_name: [('1', 'One', 'Other' if different_values else '一', 'Uno', None, None),
                                   ('2', 'Two', 'Other two' if different_values else '二', 'Dos', '1', '1')]}
    mapping = {'source_id': source_id, 'table': 'sheet'}
    if project is not None:
        mapping['applicability'] = {'status': 'scoped', 'values': {'collection': project}}
    draft = {'draft_id': cid, 'entities': [{'type': 'Entry', 'key_names': ['id'], 'mappings': [
        {**mapping, 'key_columns': ['code'], 'label_column': primary, 'alias_columns': aliases}]}],
        'relations': [{'type': 'child_of', 'source_type': 'Entry', 'target_type': 'Entry', 'mappings': [
            {**mapping, 'source_key_columns': ['code'], 'target_key_columns': [parent],
             'predicate': {'op': 'is_not_null', 'column': parent}}]}]}
    with MemorySource([table], rows) as source:
        result = execute_draft(draft, [source], [table])
    assert result['passed'], result['diagnostics']
    return {'contributor_id': cid, 'graph': result['graph']}


@pytest.mark.parametrize('project', ['M', None], ids=['scoped', 'unknown'])
def test_reciprocal_observed_names_share_the_source_key_and_retain_both_observations(project):
    first = _unit('a', 'first', ['second'], project=project)
    second = _unit('b', 'second', ['first'], project=project)
    clean = deepcopy([first, second])
    graph = merge_construction_graphs([first, second])
    assert [first, second] == clean
    assert graph == merge_construction_graphs([second, first])
    assert len(graph['nodes']) == 2 and len(graph['edges']) == 1
    assert graph['fusion_diagnostics']['source_replay_collapsed_nodes'] == 2
    assert graph['fusion_diagnostics']['source_replay_cross_contributor_source_witness_groups'] == 2
    by_id = {n['id']: n for n in graph['nodes']}
    edge, = graph['edges']
    assert [by_id[edge[end]]['identity']['id'] for end in ('source', 'target')] == ['2', '1']
    assert {(s['provenance']['label_column'], s['label'], tuple(s['aliases']))
            for s in by_id[edge['source']]['supports']} == {('first', 'Two', ('二',)), ('second', '二', ('Two',))}
    assert len(merge_construction_graphs([first, second], recognize_source_replays=False)['nodes']) == 4

    third = _unit('c', 'first', ['second'], project=project)
    old = merge_construction_graphs([first, third])
    old_ids = {n['id'] for n in old['nodes']}
    assert {n['id'] for n in merge_construction_graphs([first, second, third])['nodes']} == old_ids
    fourth = _unit('d', 'second', ['first'], project=project)
    all_four = merge_construction_graphs([first, second, third, fourth])
    assert len(all_four['nodes']) == 2 and len(all_four['edges']) == 1
    assert all_four == merge_construction_graphs([second, fourth, first, third])
    for prior in (old, graph):
        base = {'contributor_id': 'previous', 'kind': 'base', 'graph': prior, 'local_source_namespace': True}
        updated = merge_construction_graphs([base, second])
        assert {n['id'] for n in updated['nodes']} == {n['id'] for n in prior['nodes']}
        assert len(updated['edges']) == 1


@pytest.mark.parametrize('difference', ['one_way', 'ambiguous_alias_values', 'changed_name', 'source',
                                        'project', 'visibility', 'directional_role'])
def test_primary_name_change_needs_reciprocal_source_evidence_within_every_boundary(difference):
    first = _unit('a', 'first', ['second'])
    aliases = {'one_way': [], 'ambiguous_alias_values': ['first', 'third']}.get(difference, ['first'])
    second = _unit('b', 'second', aliases, different_values=difference == 'changed_name',
                   source_id='other' if difference == 'source' else 'file',
                   project='K' if difference == 'project' else 'M',
                   parent='other_parent' if difference == 'directional_role' else 'parent')
    if difference == 'visibility':
        for node in second['graph']['nodes']:
            for support in node['supports']:
                support['scope_ref'] = 'private'
    graph = merge_construction_graphs([first, second])
    assert len(graph['nodes']) == 4 and len(graph['edges']) == 2


def test_shared_primary_replay_does_not_transfer_authority_between_two_different_name_pairs():
    en_zh = _unit('a', 'first', ['second'])
    zh_en = _unit('b', 'second', ['first'])
    zh_es = _unit('c', 'second', ['third'])
    es_zh = _unit('d', 'third', ['second'])
    baseline = merge_construction_graphs([zh_en, zh_es])
    units = [en_zh, zh_en, zh_es, es_zh]
    graph = merge_construction_graphs(units)
    assert graph == merge_construction_graphs(units[::-1])
    assert len(graph['nodes']) == 6 and len(graph['edges']) == 3
    assert {n['id'] for n in baseline['nodes']} <= {n['id'] for n in graph['nodes']}
