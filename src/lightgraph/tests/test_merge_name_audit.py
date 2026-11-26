from copy import deepcopy

from lightgraph.construction.merge_name_audit import MergeNameAudit, name_population, name_rows
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.tests.test_hierarchy_projection_variants import _projection, contributor


def test_four_contributors_include_two_drafts_in_one_branch_and_keep_both_homonymous_codes():
    units = [contributor('first', _projection(0), _projection(1))]
    units += [contributor(f'unit{i}', _projection(i)) for i in (2, 3, 4)]
    audit = MergeNameAudit(units, 'Supplies')
    before = audit.inputs()
    assert (before['matched_nodes'], before['distinct_typed_keys_with_exact_label']) == (10, 2)
    assert [c['matched_nodes'] for c in before['by_contributor'].values()] == [4, 2, 2, 2]
    assert before['by_contributor']['first']['by_draft_version'] == [
        {'draft_id': 'map0', 'revision': 1, 'matching_input_nodes': 2},
        {'draft_id': 'map1', 'revision': 1, 'matching_input_nodes': 2}]
    for node in before['by_contributor']['first']['nodes']:
        assert node['source_bindings'] and all(b['key_columns'] and b['table'] for b in node['source_bindings'])
    assert all(sum(k['nodes_by_type'].values()) == k['matched_nodes'] for k in before['by_typed_key'])

    mapping = {}
    def observe(kind, owner, original, final):
        assert kind == 'unit'
        mapping[owner, original] = final

    after = audit.replay(merge_construction_graphs(units, observe_node_lineage=observe), mapping)
    assert after['matched_nodes'] == after['distinct_typed_keys'] == 2
    assert after['outputs_by_matching_input_contributors'] == {'within_and_across_contributors': 2}
    assert all((m['input_nodes'], m['distinct_output_nodes']) == (5, 1)
               for m in after['input_to_output_by_key_and_names'])


def test_name_accounting_distinguishes_display_mixed_observations_typed_keys_and_unknown_lineage():
    def node(node_id, value, kind, label, display, names):
        return {'id': node_id, 'type': 'Unit', 'identity': {'code': value}, 'identity_types': {'code': kind},
                'label': label, 'display_label': display,
                'supports': [{'observed_label': name, 'provenance': {'label_column': 'caption'}}
                             for name in names]}

    def graph(*rows):
        return {'definitions': {'entities': [{'type': 'Unit', 'key_names': ['code']}]}, 'nodes': list(rows)}

    mixed = node('same_id', 1, 'integer', 'Supplies13', 'Supplies13', ['Supplies', 'Supplies13', 'Supplies'])
    display_only = node('same_id', '1', 'text', 'generated', 'Supplies', [])
    incomplete = node('unknown', '2', 'text', 'Supplies', 'Supplies', [])
    incomplete.pop('identity_types')
    units = [{'contributor_id': 'left', 'graph': graph(mixed)},
             {'contributor_id': 'right', 'graph': graph(display_only, incomplete)}]
    audit = MergeNameAudit(units, 'Supplies')
    before = audit.inputs()
    assert (before['matched_nodes'], before['exact_display_nodes'], before['exact_observed_nodes']) == (3, 2, 1)
    assert (before['multiple_observed_names_nodes'], before['missing_typed_key_nodes']) == (1, 1)
    assert before['distinct_typed_keys'] == 2
    assert before['by_contributor']['right']['nodes_missing_draft_attribution'] == 2
    assert not name_population(name_rows(graph(mixed), 'Suppl'), 'Suppl')['matched_nodes']

    result = deepcopy(mixed)
    result['id'] = 'out'
    after = audit.replay(graph(result), {('left', 'same_id'): 'out', ('right', 'same_id'): None})
    assert [(r['contributor_id'], r['input_node_id'], r['status']) for r in after['input_mapping']] == [
        ('left', 'same_id', 'mapped'), ('right', 'same_id', 'no_output'), ('right', 'unknown', 'unverified')]
    assert after['mapped_input_nodes'] == 1
