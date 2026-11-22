"""A catalog address belongs to the project in which its source was registered."""
from copy import deepcopy

from lightgraph.construction.checkpoints import commit_checkpoint, load_checkpoint
from lightgraph.construction.executor import execute_draft
from lightgraph.construction.inheritance import _namespace_graph_evidence
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.sources import MemorySource
from lightgraph.tests.test_construction_metadata_columns import physical_table, row_draft


def _publication():
    table = physical_table()
    with MemorySource([table], {table.qualified_name: [('A', 'first', 'B', 'second', 'public', 1)]}) as source:
        checked = execute_draft(row_draft(), [source], [table])
    assert checked['passed'], checked['diagnostics']
    return checked['graph']


def _columns(graph):
    return [node for node in graph['nodes'] if node.get('identity_origin') == 'physical_column']


def test_worker_reobserves_catalog_column_from_local_checkpoint(tmp_path):
    graph = _publication()
    upstream = merge_construction_graphs([{'contributor_id': 'extract', 'graph': graph}])
    cid = _columns(upstream)[0]['id']
    record = commit_checkpoint(tmp_path, 'run_' + 'a' * 24, 'previous',
                               {'kind': 'merged', 'graph': upstream}, input_digest='test')
    upstream = load_checkpoint(tmp_path, record)['graph']
    delta = {'contributor_id': 'delta', 'graph': graph}
    merged = merge_construction_graphs([
        {'kind': 'base', 'contributor_id': 'up', 'local_source_namespace': True, 'graph': upstream}, delta])
    column, = _columns(merged)
    assert column['id'] == cid
    assert {s['contributor_id'] for s in column['supports']} | {
        o['contributor_id'] for s in column['supports'] for o in s.get('execution_origins', [])} >= {'extract', 'delta'}
    assert {edge['target'] for edge in merged['edges']} == {cid}
    replay = merge_construction_graphs([
        {'kind': 'base', 'contributor_id': 'up', 'local_source_namespace': True, 'graph': merged}, delta])
    assert [n['id'] for n in _columns(replay)] == [cid]
    assert replay['edges'] == merged['edges']


def test_foreign_catalog_collision_keeps_one_local_column_and_separate_edges():
    graph = _publication()
    foreign = _namespace_graph_evidence(deepcopy(graph), inherit_id='other', source_project_id='elsewhere',
                                        source_digest='pinned')
    base = {'kind': 'base', 'contributor_id': 'other', 'graph': foreign}
    unit = {'contributor_id': 'one', 'graph': graph}
    second = {'contributor_id': 'two', 'graph': graph}
    together = merge_construction_graphs([base, unit, second])
    assert len(_columns(together)) == 2
    first = merge_construction_graphs([base, unit])
    staged = merge_construction_graphs([{'contributor_id': 'first', 'kind': 'merged', 'graph': first}, second])
    expected = {c['id'] for c in _columns(together)}
    assert {c['id'] for c in _columns(first)} == {c['id'] for c in _columns(staged)} == expected
    for result in (together, staged):
        by_id = {n['id']: n for n in result['nodes']}
        for edge in result['edges']:
            foreign_edge = {bool(s.get('inherited_from')) for s in edge['supports']}
            assert len(foreign_edge) == 1
            for end in ('source', 'target'):
                assert {bool(s.get('inherited_from')) for s in by_id[edge[end]]['supports']} == foreign_edge
