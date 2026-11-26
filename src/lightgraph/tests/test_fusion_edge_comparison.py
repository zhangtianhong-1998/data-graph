from copy import deepcopy
from pathlib import Path

import pytest

from lightgraph.construction.checkpoints import commit_checkpoint
from lightgraph.construction.workflow import save_workflow_state
from lightgraph.tests.test_fusion_census_edges import _verified_edge
from lightgraph.tests.test_fusion_committed_comparison import invoke, load, save


def graph(edges):
    return {'nodes': [{'id': n, 'type': 'Place', 'identity': {'code': code}, 'identity_types': {'code': 'text'},
                       'label': 'Same display name', 'supports': [{
                           'provenance': {'source': {'source_id': 'orders', 'schema': 'sales', 'table': 'Sheet1'},
                                          'key_columns': [col]}}]}
                      for n, code, col in [('p', '00', 'AREA_L1_ID'), ('c', '01', 'AREA_L2_ID')]],
            'edges': edges, 'definitions': {'entities': [{'type': 'Place', 'key_names': ['code']}],
                                          'relations': [{'type': t, 'source_type': 'Place', 'target_type': 'Place'}
                                                        for t in sorted({e['type'] for e in edges})]}}


def archive(project, rid, g):
    out = project / 'output'
    out.mkdir(exist_ok=True)
    (project / 'runs').mkdir(exist_ok=True)
    merged = commit_checkpoint(out, rid, 'merge_1', {'kind': 'merged', 'graph': g}, input_digest='fixed')
    save_workflow_state(out, {'parent_run_id': rid, 'artifacts': {'merge_1': merged}})
    path = project / 'runs' / (rid + '.json')
    save(path, {'run_id': rid, 'project_id': 'example', 'output_path': 'output'})
    return path


def test_cli_settles_every_raw_edge_without_declaring_an_unmatched_business_fact_lost(tmp_path):
    a = _verified_edge('old', 'contains_area', [[2, 3]])
    copy = {**deepcopy(a), 'id': 'duplicate_observation'}
    bare = _verified_edge('bare', 'contains_area', [[5, 5]])
    del bare['supports'][0]['source_row_witness']
    loop = {**deepcopy(bare), 'id': 'loop', 'source': 'c'}
    old = archive(tmp_path, 'run_' + '1' * 24, graph([a, copy, bare, loop]))
    b = _verified_edge('renamed_id', 'contains_area', [[2, 2], [3, 3]])
    c = _verified_edge('new_meaning', 'bills_to', [[2, 3]])
    other = _verified_edge('other_rows', 'contains_area', [[4, 4]])
    new = archive(tmp_path, 'run_' + '2' * 24, graph([b, c, other]))
    path = tmp_path / 'comparison.json'
    invoke('compare-source-relations', old, new, '--result', path)
    result = load(path)
    assert result['old']['raw_edges_by_status'] == {
        'same_qualified_observation_and_contract': 2, 'same_qualified_observation_other_contract': 0,
        'observed_on_one_side_only': 0, 'insufficient_evidence_or_self_loop': 2}
    assert result['new']['raw_edges_by_status'] == {
        'same_qualified_observation_and_contract': 1, 'same_qualified_observation_other_contract': 1,
        'observed_on_one_side_only': 1, 'insufficient_evidence_or_self_loop': 0}
    assert result['old']['unverified_edge_ids_by_reason'] == {
        'missing_or_invalid_physical_rows': ['bare'], 'self_loop_retained': ['loop']}
    assert result['old']['raw_edges'] == 4 and result['new']['raw_edges'] == 3
    assert result['old']['first_failure_counts'] == result['old']['unverified_counts_by_reason']
    assert result['old']['evidence_coverage']['assessable_edges'] == 2
    assert result['old']['evidence_coverage']['assessable_coverage'] == .5
    assert result['new']['evidence_coverage']['assessable_coverage'] == 1
    assert result['analysis_code']['sha256']


@pytest.mark.parametrize('change', ['source', 'columns', 'type', 'code', 'direction', 'scope', 'snapshot'])
def test_projection_differences_do_not_match_by_label_or_endpoint_presence(monkeypatch, change):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / 'scripts'))
    from fusion_edge_comparison import _index
    first = graph([_verified_edge('edge', 'contains_area', [[2, 3]])])
    other = deepcopy(first)
    edge = other['edges'][0]
    support = edge['supports'][0]
    prov = support['provenance']
    if change == 'columns':
        prov['target_key_columns'] = ['OTHER']
        prov['source_column_ordinals']['OTHER'] = 7
    elif change == 'source':
        prov['source']['table'] = 'other'
    elif change == 'type':
        other['nodes'][0].update(identity={'code': 0}, identity_types={'code': 'integer'})
    elif change == 'code':
        other['nodes'][0]['identity']['code'] = 'elsewhere'
    elif change == 'direction':
        edge['source'], edge['target'] = edge['target'], edge['source']
    elif change == 'scope':
        support['required_scope_refs'] = ['restricted']
    else:
        support['source_row_witness']['snapshot'] = 'f' * 64
    assert len(_index(first)[1]) == 1
    assert set(_index(first)[1]).isdisjoint(_index(other)[1])


@pytest.mark.parametrize('change', ['no_node_support', 'other_column', 'other_table', 'source_key_value',
                                    'source_key_type', 'remap_missing_key', 'node_row', 'one_edge_support',
                                    'same_column_conflicting_value'])
def test_each_edge_support_requires_an_uncontradicted_endpoint_binding(monkeypatch, change):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / 'scripts'))
    from fusion_edge_comparison import _index
    data = graph([_verified_edge('edge', 'contains_area', [[2, 3]])])
    source = data['nodes'][0]
    support = source['supports'][0]
    column = 'AREA_L1_ID'
    if change == 'no_node_support':
        source['supports'] = []
    elif change == 'other_column':
        support['provenance']['key_columns'] = ['OTHER_ID']
    elif change == 'other_table':
        support['provenance']['source']['table'] = 'Other'
    elif change in ('source_key_value', 'source_key_type', 'same_column_conflicting_value'):
        if change == 'same_column_conflicting_value':
            support = deepcopy(support)
            source['supports'].append(support)
        support.update(original_key_values={column: '01' if change != 'source_key_type' else '00'},
                       original_key_types={column: 'text' if change != 'source_key_type' else 'decimal'})
    elif change == 'remap_missing_key':
        support['identity_origins'] = [{'identity': {'old': '00'}}]
    elif change == 'node_row':
        support['row_context'] = {'values': {column: '03'}, 'value_types': {column: 'text'}}
    else:
        edge_support = deepcopy(data['edges'][0]['supports'][0])
        edge_support['row_context'] = {'values': {column: '03'}, 'value_types': {column: 'text'}}
        data['edges'][0]['supports'].append(edge_support)
    count, groups, unknown = _index(data)
    assert count == 1 and groups == {} and unknown == {'unverified_physical_endpoint': ['edge']}


def test_composite_endpoint_with_reversed_physical_columns_cannot_witness_the_edge(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / 'scripts'))
    from fusion_edge_comparison import _index
    data = graph([_verified_edge('edge', 'contains_area', [[2, 3]])])
    data['definitions']['entities'][0]['key_names'].append('unit')
    edge_prov = data['edges'][0]['supports'][0]['provenance']
    for side, node in zip(('source', 'target'), data['nodes'], strict=True):
        node['identity']['unit'] = 'u'
        node['identity_types']['unit'] = 'text'
        columns = node['supports'][0]['provenance']['key_columns']
        columns.append('UNIT')
        edge_prov[side + '_key_columns'].append('UNIT')
        node['supports'][0].update(original_key_values=dict(zip(columns, node['identity'].values(), strict=True)),
                                   original_key_types=dict.fromkeys(columns, 'text'))
    edge_prov['source_column_ordinals']['UNIT'] = 9
    count, groups, unknown = _index(data)
    assert count == 1 and len(groups) == 1 and not unknown
    data['nodes'][0]['supports'][0]['provenance']['key_columns'].reverse()
    count, groups, unknown = _index(data)
    assert count == 1 and groups == {} and unknown == {'unverified_physical_endpoint': ['edge']}


def test_all_blockers_and_first_failure_buckets_have_separate_denominators(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / 'scripts'))
    from fusion_edge_comparison import _diagnostic_summary, _index

    missing_both = _verified_edge('missing-both', 'contains_area', [[2, 3]])
    del missing_both['supports'][0]['source_row_witness']
    del missing_both['supports'][0]['business_context']
    missing_scope = _verified_edge('missing-scope', 'contains_area', [[4, 4]])
    del missing_scope['supports'][0]['business_context']
    malformed = {'id': 'missing-provenance', 'type': 'contains_area', 'source': 'p', 'target': 'c',
                 'supports': [{'scope_ref': 'public'}]}
    count, groups, first, diagnostics = _index(graph([missing_both, missing_scope, malformed]), with_diagnostics=True)
    assert count == 3 and not groups and sum(map(len, first.values())) == 3
    assert first['missing_or_invalid_physical_rows'] == ['missing-both']
    assert first['unknown_access_or_business_scope'] == ['missing-scope']
    result = _diagnostic_summary(diagnostics)
    assert result['all_blocker_edge_counts']['unknown_access_or_business_scope'] == 2
    assert result['all_blocker_edge_counts']['missing_or_invalid_physical_rows'] == 1
    assert result['assessable_coverage'] == 0 and result['status'] == 'no_assessable_evidence'
    assert result['raw_edge_denominator'] == result['support_denominator'] == 3
    incomplete = next(row for row in diagnostics if row['edge_id'] == 'missing-provenance')
    assert any(check['check'] == 'scope' and check['status'] == 'not_checked'
               for check in incomplete['supports'][0]['checks'])
    assert result['check_counts']['support.scope'] == {'passed': 0, 'failed': 2, 'not_checked': 1}


def test_projection_tuple_evidence_is_assessable_without_worksheet_rows(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / 'scripts'))
    from fusion_edge_comparison import _diagnostic_summary, _index
    data = graph([_verified_edge('edge', 'contains_area', [[2, 3]])])
    support = data['edges'][0]['supports'][0]
    del support['source_row_witness']
    del support['provenance']['source_column_ordinals']
    support['provenance']['columns'] = ['AREA_L1_ID', 'AREA_L2_ID']
    support['source_projection_witness'] = {
        'kind': 'source_projection_v1', 'basis': 'complete_distinct_projection', 'complete': True,
        'source_binding': 'a' * 64, 'projection': 'b' * 64, 'tuple': 'c' * 64,
        'columns': ['AREA_L1_ID', 'AREA_L2_ID']}
    count, groups, failures, details = _index(data, with_diagnostics=True)
    assert count == len(groups) == 1 and not failures
    coverage = _diagnostic_summary(details)
    assert coverage['assessable_coverage'] == 1
    assert coverage['witness_kind_support_counts'] == {'source_projection_v1': 1}
