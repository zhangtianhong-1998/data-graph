"""Two file formats and vocabularies exercise extraction, merge and audit provenance."""
import csv
from pathlib import Path
import runpy

import openpyxl
import pytest

from lightgraph.construction.executor import execute_draft
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.construction.workspace import _merge_graphs
from lightgraph.runtime.config import FusionPolicyConfig
from lightgraph.sources.csv import CSVSource
from lightgraph.sources.excel import ExcelSource
from lightgraph.tests.test_hierarchy_projection_variants import _facts


@pytest.mark.parametrize('extension, columns, kinds', [
    ('csv', ('section', 'heading', 'item', 'caption'), ('Subject', 'Entry', 'Term')),
    ('xlsx', ('设备组编号', '设备组名称', '部件编码', '部件名称'), ('部件', '设备', '元件')),
])
def test_disconnected_members_keep_their_evidence_across_file_formats(tmp_path, extension, columns, kinds):
    path = tmp_path / f'catalog.{extension}'
    rows = [columns, ('P', 'Alpha', 'C', 'Beta'), ('S', 'Gamma', 'S', 'Gamma')]
    if extension == 'csv':
        with path.open('w', encoding='utf-8', newline='') as out:
            csv.writer(out).writerows(rows)
        adapter = CSVSource(path, source_id='catalog', encoding='utf-8', delimiter=',', header=True,
                            null_values=[], type_inference='text')
    else:
        book = openpyxl.Workbook()
        book.active.title = '设备目录'
        for row in rows:
            book.active.append(row)
        book.save(path)
        book.close()
        adapter = ExcelSource(path, source_id='catalog')

    units = []
    with adapter:
        table, = adapter.describe()
        source = {'source_id': table.source_id, 'table': table.name}
        for i, kind in enumerate(kinds):
            left, right = (columns[2], columns[0]) if i == 1 else (columns[0], columns[2])
            draft = {'draft_id': f'draft{i}', 'entities': [
                {'type': kind, 'key_names': [f'key{i}'], 'identity_scope': 'global',
                 'identity_domain': kind, 'identity_evidence_refs': ['e1'],
                 'mappings': [{**source, 'key_columns': [k], 'label_column': v}
                              for k, v in (columns[:2], columns[2:])]}], 'relations': [
                {'type': f'link{i}', 'source_type': kind, 'target_type': kind, 'allow_self_links': i == 0,
                 'hierarchy_orientation': 'child_to_parent' if i == 1 else 'parent_to_child',
                 'mappings': [{**source, 'source_key_columns': [left], 'target_key_columns': [right],
                               'equal_endpoints': 'include' if i == 0 else 'omit'}]}]}
            result = execute_draft(draft, [adapter], [table])
            assert result['passed'], result['diagnostics']
            published = dict(result['graph'], draft_id=draft['draft_id'], draft_revision=1)
            units.append({'contributor_id': f'u{i}', 'graph': _merge_graphs([published], fusion_policy=FusionPolicyConfig()),
                          'evidence_records': {'e1': {'kind': 'source_probe', 'source_ref': f'catalog.{table.qualified_name}'}}})

    control = merge_construction_graphs(units, recognize_source_replays=False)
    replay = merge_construction_graphs(units)
    assert (len(control['nodes']), len(replay['nodes']), len(replay['edges'])) == (9, 3, 4)
    assert _facts(control) == _facts(replay)
    diag = replay['fusion_diagnostics']['hierarchy_replay_diagnostics']
    assert diag['collapsed_nodes_by_role_evidence'] == {'direct_neighbors': 4, 'isolated_member_role': 2}
    assert replay == merge_construction_graphs(units[::-1])

    audit = runpy.run_path(str(Path(__file__).resolve().parents[3] / 'scripts/audit_checkpoint_merge.py'))
    membership = audit['hierarchy_membership'](replay, {n['id'] for n in control['nodes']})
    assert membership['agrees_with_recorded_count'] and membership['represented_collapsed_nodes'] == 6
    def source_code(node):
        return next(iter(node['identity'].values()))
    for node in replay['nodes']:
        expected = {n['id'] for n in control['nodes'] if source_code(n) == source_code(node)}
        assert set(membership['groups_by_output_id'][node['id']]) == expected
