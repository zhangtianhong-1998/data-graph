from copy import deepcopy

import pytest

from lightgraph.construction.access import filter_graph_scopes
from lightgraph.construction.graph_storage import read_graph_record, write_graph_record
from lightgraph.construction.hierarchy_stage import RECORD
from lightgraph.construction.merger import merge_construction_graphs, write_sharded_graph
from lightgraph.tests.test_source_hierarchy_replays import contributor, draft_graph


def units():
    return [dict(contributor(name, draft_graph(number, kind, inverse=number == 2)), source_namespace='library')
            for number, (name, kind) in enumerate([('one', 'Entry'), ('two', 'Term'), ('three', 'Subject')], 1)]


def stage(inputs, **kwargs):
    return dict(contributor_id='previous', kind='merged', source_namespace='library',
                graph=merge_construction_graphs(inputs, **kwargs))


@pytest.mark.parametrize('root_publication', [False, True])
def test_staged_hierarchy_from_published_observations_matches_all_original_leaves(tmp_path, root_publication):
    all_units = units()
    intermediate = stage(all_units[:2])
    snapshot = deepcopy(intermediate)
    if root_publication:
        publication = write_sharded_graph(tmp_path, intermediate['graph'], context_fingerprint='library')
        intermediate['graph'] = read_graph_record(tmp_path / publication['core_path'])
        snapshot['graph']['storage_layout'] = 'parquet_v1'
    else:
        write_graph_record(tmp_path / 'stage.graph', intermediate['graph'])
        intermediate['graph'] = read_graph_record(tmp_path / 'stage.graph')
    assert len(intermediate['graph']['nodes']) == 3
    record = intermediate['graph'][RECORD]
    for declaration in record['units'].values():
        assert not {'nodes', 'edges', 'supports'} & set(declaration['header'])
    assert RECORD not in filter_graph_scopes(intermediate['graph'], [])
    assert RECORD not in filter_graph_scopes(intermediate['graph'], ['public'])
    assert 'physical_source_namespace_binding' not in filter_graph_scopes(intermediate['graph'], [])
    merged = merge_construction_graphs([intermediate, all_units[2]])
    assert (len(merged['nodes']), len(merged['edges'])) == (3, 6)
    assert merged == merge_construction_graphs(all_units[::-1])
    assert intermediate == snapshot


@pytest.mark.parametrize('direction,count', [('none', 6), ('child_to_parent', 9)])
def test_new_role_is_checked_against_every_original_even_if_it_contradicts_previous_fold(direction, count):
    all_units = units()
    intermediate = stage(all_units[:2])
    all_units[2]['graph']['definitions']['relations'][0]['hierarchy_orientation'] = direction
    result = merge_construction_graphs([intermediate, all_units[2]])
    assert len(result['nodes']) == count
    assert result == merge_construction_graphs(all_units)


@pytest.mark.parametrize('change', [
    'no_namespace', 'foreign_namespace', 'foreign_unit', 'explicit_default_policy', 'base', 'inherited',
    'inherited_metadata', 'different_access', 'changed_output', 'legacy', 'modified_leaf', 'incomplete_leaves',
    'disabled', 'independent', 'changed_storage_layout', 'changed_publication_header',
])
def test_ineligible_stages_remain_frozen(change):
    a, b, c = units()
    prior = stage([a, b])
    options = {}
    if change == 'no_namespace':
        del prior['source_namespace']
    elif change == 'foreign_namespace':
        prior['source_namespace'] = c['source_namespace'] = 'museum'
    elif change == 'foreign_unit':
        c['source_namespace'] = 'museum'
    elif change == 'explicit_default_policy':
        options['merge_policy'] = {'conflict_mode': 'quarantine'}
    elif change == 'base':
        prior['kind'] = 'base'
    elif change == 'inherited':
        c['graph']['extends_base_graph'] = True
    elif change == 'inherited_metadata':
        c['graph']['nodes'][0]['supports'][0]['inherited_from'] = {'project': 'other'}
    elif change == 'different_access':
        options['target_access_fingerprint'] = 'another-access-policy'
    elif change == 'changed_output':
        prior['graph']['storage_layout'] = 'parquet_v1'
        prior['graph']['edges'][0]['supports'][0]['properties']['extra'] = ['keep']
    elif change == 'changed_storage_layout':
        prior['graph']['storage_layout'] = 'other'
    elif change == 'changed_publication_header':
        prior['graph'].update(storage_layout='parquet_v1', semantic_origin='another')
    elif change == 'legacy':
        del prior['graph'][RECORD]
    elif change == 'modified_leaf':
        prior['original_units'] = [a, b]
        a['evidence_records']['e1']['kind'] = 'document_section'
    elif change == 'incomplete_leaves':
        prior['original_units'] = [a]
    elif change == 'disabled':
        options['recognize_source_replays'] = False
    elif change == 'independent':
        options['independent_outputs'] = True
    graph = merge_construction_graphs([prior, c], **options)
    assert (len(graph['nodes']), len(graph['edges'])) == (6, 6)
    assert RECORD not in graph
    if change == 'changed_output':
        assert any(s['properties'].get('extra') == ['keep'] for e in graph['edges'] for s in e['supports'])


def test_exact_external_leaves_can_supply_a_contract_outside_the_reversible_format():
    a, b, c = units()
    # A validation snapshot can contain arbitrary audit information. The recipe
    # deliberately keeps just a digest for this unit rather than copying its data.
    a['graph']['validation_snapshots'] = [{'draft_id': 'original', 'read_window': {'revision': 3}}]
    prior = stage([a, b])
    assert set(prior['graph'][RECORD]['units']['one']) == {'digest'}
    assert len(merge_construction_graphs([prior, c])['nodes']) == 6
    prior['original_units'] = [a, b]
    assert merge_construction_graphs([prior, c]) == merge_construction_graphs([a, b, c])


def test_explicit_foreign_namespace_disables_physical_hierarchy_fold_in_fresh_stage():
    a, b, _ = units()
    b['source_namespace'] = 'museum'
    merged = merge_construction_graphs([a, b])
    assert len(merged['nodes']) == 6
    assert RECORD not in merged


def test_verified_originals_can_recheck_a_stage_that_initially_lacked_direction():
    a, b, c = units()
    a['graph']['definitions']['relations'][0].pop('hierarchy_orientation')
    # Both undecided units observed the same column order. The third unit records its direction.
    b = dict(contributor('two', draft_graph(2, 'Term', declare_direction=False)), source_namespace='library')
    prior = stage([a, b])
    assert len(prior['graph']['nodes']) == 6
    prior['original_units'] = [a, b]
    result = merge_construction_graphs([prior, c])
    assert (len(result['nodes']), len(result['edges'])) == (3, 6)
    assert result == merge_construction_graphs([a, b, c])


def test_new_input_can_match_one_of_two_previously_disjoint_hierarchies():
    a, _, c = units()
    b = dict(contributor('two', draft_graph(2, 'Term', code_prefix='unrelated-')), source_namespace='library')
    prior = stage([a, b])
    assert len(prior['graph']['nodes']) == 6
    prior['original_units'] = [a, b]
    result = merge_construction_graphs([prior, c])
    assert (len(result['nodes']), len(result['edges'])) == (6, 6)
    assert result == merge_construction_graphs([a, b, c])
