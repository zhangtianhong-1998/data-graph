from collections import Counter

from lightgraph.construction.executor import execute_draft
from lightgraph.construction.hierarchy_trace import HierarchyTrace
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.sources import MemorySource
from lightgraph.tests.test_source_hierarchy_replays import contributor


def test_four_projections_of_a_directory_keyed_by_code_and_name_preserve_each_childs_parent():
    parent_key = ['parent_no', 'parent_name']
    child_key = ['child_no', 'child_name']
    table = TableSpec(source_id='ledger', name='directory', columns=[
        ColumnSpec(name=c) for c in [*parent_key, *child_key]])
    rows = [('P1', '甲类', 'X', '拨备'), ('P2', '乙类', 'X', '拨备13')]
    binding = {'source_id': table.source_id, 'table': table.name}
    units = []
    for index, kind in enumerate(['Entry', 'Account', 'Measure', 'Item']):
        draft = {
            'draft_id': f'projection{index}',
            'entities': [{'type': kind, 'key_names': ['code', 'name'], 'identity_scope': 'global',
                          'identity_domain': 'ledger', 'identity_evidence_refs': [f'e{index}'],
                          'mappings': [{**binding, 'key_columns': cols, 'label_column': cols[1]}
                                       for cols in (parent_key, child_key)]}],
            'relations': [{'type': f'parent{index}', 'source_type': kind, 'target_type': kind,
                           'hierarchy_orientation': 'parent_to_child', 'mappings': [
                               {**binding, 'source_key_columns': parent_key, 'target_key_columns': child_key}]}]}
        with MemorySource([table], {table.qualified_name: rows}) as adapter:
            result = execute_draft(draft, [adapter], [table])
        assert result['passed'], result['diagnostics']
        graph = result['graph']
        graph.update(draft_id=draft['draft_id'], draft_revision=1)
        units.append(contributor(f'unit{index}', graph))
    assert all(len(u['graph']['nodes']) == 4 for u in units)
    trace = HierarchyTrace('X', key_field='code')
    merged = merge_construction_graphs(units, hierarchy_trace=trace)
    by_id = {n['id']: (n['identity']['code'], n['identity']['name']) for n in merged['nodes']}
    assert len(by_id) == 4
    assert {tuple(g['observed_names']): (len(g['input_node_ids']), len(g['output_node_ids']))
            for g in trace.report(merged)['observed_name_sets']} == {('拨备',): (4, 1), ('拨备13',): (4, 1)}
    assert Counter((by_id[e['source']], by_id[e['target']]) for e in merged['edges']) == {
        (('P1', '甲类'), ('X', '拨备')): 4, (('P2', '乙类'), ('X', '拨备13')): 4}
    for node in merged['nodes']:
        contributors = {o['contributor_id'] for s in node['supports'] for o in s['execution_origins']}
        assert contributors == {u['contributor_id'] for u in units}
