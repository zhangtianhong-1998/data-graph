from lightgraph.construction.hierarchy_trace import HierarchyTrace
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.construction.workspace import _merge_graphs
from lightgraph.runtime.config import FusionPolicyConfig
from lightgraph.tests.test_hierarchy_projection_variants import _projection, contributor


def _units(conflict):
    units = []
    for name, indices in [('first', [0, 1]), ('second', [2])]:
        drafts = [_projection(i, conflict=conflict) for i in indices]
        unit = contributor(name, *drafts)
        unit['graph'] = _merge_graphs(drafts, fusion_policy=FusionPolicyConfig(label_conflict_policy='retain_variants'))
        units.append(unit)
    return units


def test_trace_uses_the_rejection_for_the_requested_code_and_preserves_draft_sources():
    trace = HierarchyTrace('S01')
    graph = merge_construction_graphs(_units(True), hierarchy_trace=trace)
    result = trace.report(graph)
    assert result['hierarchy_stage_ran']
    inputs = result['nodes_before_hierarchy']
    assert len(inputs) == len(result['output_ids']) == 3
    assert {tuple(n['identity'].values()) for n in inputs.values()} == {('S01',)}
    assert {b['unit'] for n in inputs.values() for b in n['source_bindings']} == {'first', 'second'}
    assert {b['draft_id'] for n in inputs.values() for b in n['source_bindings']} == {'map0', 'map1', 'map2'}
    assert {ref['kind'] for n in inputs.values() for ref in n['identity_evidence']} == {'source_probe'}
    assert {b['observed_label'] for n in inputs.values() for b in n['source_bindings']} == {'Supplies', 'Supply items'}
    name_set, = result['observed_name_sets']
    assert name_set['observed_names'] == ['Supplies', 'Supply items']
    assert name_set['input_node_ids'] == sorted(inputs)
    assert name_set['output_node_ids'] == result['output_ids']
    assert name_set['unverified_input_node_count'] == 0 and not name_set['incomplete_name_evidence']
    assert all(b['row_locator'] is None for n in inputs.values() for b in n['source_bindings'])
    assert any(e['result'] == 'multiple_labels' and set(e['node_ids']) == set(inputs) for e in result['events'])
    assert not any(e['result'] == 'passed_before_endpoint_remap' for e in result['events'])


def test_trace_records_actual_output_membership_and_does_not_change_merge_or_match_labels():
    units = _units(False)
    expected = merge_construction_graphs(units)
    trace = HierarchyTrace('B01')
    actual = merge_construction_graphs(units, hierarchy_trace=trace)
    assert actual == expected
    result = trace.report(actual)
    assert len(result['nodes_before_hierarchy']) == 3
    assert len(result['output_ids']) == 1
    name_set, = result['observed_name_sets']
    assert name_set['observed_names'] == ['Supplies'] and len(name_set['input_node_ids']) == 3
    assert name_set['output_node_ids'] == result['output_ids']
    assert result['output_ids'][0] in {n['id'] for n in actual['nodes']}
    assert any(e['result'] == 'passed_before_endpoint_remap' for e in result['events'])
    labels = HierarchyTrace('Supplies')
    assert labels.report(merge_construction_graphs(units, hierarchy_trace=labels))['nodes_before_hierarchy'] == {}
