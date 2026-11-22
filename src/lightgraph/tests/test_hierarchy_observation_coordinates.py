"""Conservative equivalence for position annotations retained on source observations."""
from copy import deepcopy

import pytest

from lightgraph.construction.hierarchy_observations import observation_boundary, observation_properties
from lightgraph.construction.hierarchy_replay import _ancestor_paths_agree


def _source():
    return {
        'scope_ref': 'public',
        'provenance': {'source': {'table': 'vocabulary'},
                       'label_column': 'caption', 'constant_properties': {'position': 'excel_column_2'}},
        'properties': {'position': ['excel_column_2']},
    }


def test_absolute_column_position_needs_the_original_header_ordinal():
    annotated = _source()
    plain = deepcopy(annotated)
    plain['properties'] = {}
    plain['provenance']['constant_properties'] = {}
    keys = ['root']
    chain = {'root': 1, 'leaf': 2}
    match = observation_properties(plain, keys, chain)
    for ordinal in [None, {'root': 1}, {'root': '2'}, {'root': True}, {'leaf': 2}]:
        annotated['provenance']['source_column_ordinals'] = ordinal
        assert observation_properties(annotated, keys, chain) != match
    annotated['provenance']['source_column_ordinals'] = {'root': 2}
    assert observation_properties(annotated, keys, chain) == match


@pytest.mark.parametrize('label', ['L1', 'root', 'vocabulary_level1', 'excel_column_2', 1])
def test_positional_spelling_cannot_reinterpret_a_project(label):
    sample = _source()
    plain = deepcopy(sample)
    plain['properties'] = {}
    plain['provenance']['constant_properties'] = {}
    sample['provenance']['source_column_ordinals'] = {'root': 2}
    sample['provenance']['constant_properties'] = {'project': label}
    sample['properties'] = {'project': [label]}
    assert observation_properties(sample, ['root'], {'root': 1}) != observation_properties(plain, ['root'], {'root': 1})


def test_nested_predicates_keep_nonkey_filters_and_protected_path_values():
    sample = _source()
    sample['label'] = 'name'
    sample['provenance']['scope_columns'] = ['leaf']
    sample['row_context'] = {'values': {'root': 'AAA', 'leaf': 'zone A'}, 'value_types': {'root': 'text', 'leaf': 'text'}}
    baseline = deepcopy(sample)
    chain = {'root': 1, 'leaf': 2}
    def boundary(row):
        return observation_boundary(row, ['root'], chain)
    sample['provenance']['predicate'] = {'all': [{'op': 'not_null', 'column': 'caption'}, {'and': [
        {'op': 'is_not_null', 'column': 'root'}]}]}
    assert boundary(baseline) == boundary(sample)
    sample['provenance']['predicate']['all'].append({'op': 'is_not_null', 'column': 'leaf'})
    assert boundary(baseline) != boundary(sample)
    sample['provenance'].pop('predicate')
    sample['row_context']['values']['leaf'] = 'zone B'
    assert boundary(baseline) != boundary(sample)


def test_shared_ancestor_columns_must_agree_as_paths_not_individual_values():
    location = {'source_id': 'file', 'schema': 'public', 'table': 'catalog'}
    positions = {'root': 1, 'middle': 2, 'leaf': 3}

    def projection(rows):
        return {'type': 'item', 'supports': [
            {'provenance': {'source': location, 'key_columns': ['leaf']},
             'row_context': {'values': row, 'value_types': dict.fromkeys(row, 'text')}} for row in rows]}

    def agree(a, b):
        return _ancestor_paths_agree(['a', 'b'], {'a': projection(a), 'b': projection(b)},
                                     {('item', ('file', 'public', 'catalog'), ('leaf',)): positions}, {})

    rows = [{'root': 'A', 'middle': 'X'}, {'root': 'B', 'middle': 'Y'}]
    assert agree(rows, list(reversed(rows)))
    assert agree(rows, [{'middle': 'X'}, {'middle': 'Y'}])
    assert not agree(rows, [{'root': 'A', 'middle': 'Y'}, {'root': 'B', 'middle': 'X'}])

    # Some projections record all paths on edges and no row on the member.
    edge_rows = {nid: [(('leaf',), (('middle',), 'X'), {'values': {'root': value},
                        'value_types': {'root': 'text'}}, positions, None)] for nid, value in [('a', 'A'), ('b', 'B')]}
    assert not _ancestor_paths_agree(['a', 'b'], dict.fromkeys(['a', 'b'], projection([])), {}, edge_rows)
