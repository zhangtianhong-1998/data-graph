from copy import deepcopy
from pathlib import Path

import pytest

from lightgraph.tests.test_batch_overlap_patterns import unit
from lightgraph.tests.test_projection_proposals import _bind_section


@pytest.fixture
def api(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / 'scripts'))
    import fusion_evidence_keys
    return fusion_evidence_keys


def _case():
    sources = [unit(owner, 'Place', 'id', table=owner) for owner in ('a', 'b', 'c')]
    for src in sources:
        _bind_section(src, 'v1')
        src['graph']['edges'][0]['supports'][0]['business_context'] = {'status': 'global', 'dimensions': {}}
    bindings = [{'contributor_id': owner, 'entity_type': 'Place', 'source_id': 'db', 'schema': 'dm',
                 'table': owner, 'key_columns': ['code'], 'relation_type': 'parent', 'end': 'source',
                 'opposite_key_columns': ['parent_code']} for owner in ('a', 'b', 'c')]
    return sources, [{'pattern_id': 'abc', 'action': 'propose', 'selected_bindings': bindings}]


def _request(pid='abc', i=0):
    return i, pid, ('a', 'a_0'), ('b', 'b_0')


def test_all_selected_members_real_key_and_independent_evidence(api, monkeypatch):
    sources, routes = _case()
    sources[2]['graph']['access_fingerprint'] = 'other selection'
    source = sources[2]['graph']['nodes'][0]['supports'][0]
    source['label'] = 'different'
    source['provenance']['global_identity_authority']['key_sections_v1'] = []
    sources[2]['graph']['edges'] = []
    for ix, value in enumerate(('gross', 'net')):
        s = sources[ix]['graph']['nodes'][0]['supports'][0]
        s['properties']['qual'] = [value]
        s['provenance']['property_columns']['qual'] = 'qual'
        s['provenance']['identity_qualifiers'] = {'qual': 'measure_basis'}
    invoked = []
    original = api.projections._by_key

    def count(binding, data):
        invoked.append(binding.contributor_id)
        return original(binding, data)

    monkeypatch.setattr(api.projections, '_by_key', count)
    routes.append({**routes[0], 'pattern_id': 'same_bindings'})
    out = api.observed_pair_checks(sources, routes, [_request(), _request('same_bindings')])
    row = out[0, 'abc']
    assert invoked == ['a', 'b', 'c'] and row == out[0, 'same_bindings']
    assert row['status'] == 'observed_all_selected_members'
    assert row['selected_binding_match_counts'] == [1, 1, 1] and row['key_types'] == ['text']
    assert row['observed_names_equal'] is False and row['unit_fingerprints_match'] is False
    assert row['all_support_applicability_known'] is True
    assert row['shared_bound_section_ids'] == [] and row['table_grant_checked'] is False
    assert row['conflicting_identity_qualifier_values_observed'] is True
    assert row['member_observations'][0]['observed_identity_qualifier_roles'] == ['measure_basis']
    assert row['member_observations'][2]['selected_role_witnessed'] is False
    assert row['relation_and_neighbor_witnesses_equal'] is None
    assert '000071' not in str(out) and 'different' not in str(row['member_observations'])


@pytest.mark.parametrize(('case', 'status', 'counts'), [
    ('third_missing', 'key_absent_from_selected_binding', [1, 1, 0]),
    ('third_nonunique', 'nonunique_member_in_projection', [1, 1, 2]),
    ('third_incomplete', 'selected_binding_has_incomplete_typed_identity', [1, 1, None]),
    ('different', 'different_complete_typed_keys', None),
    ('unselected', 'requested_member_unverified', None),
])
def test_partial_and_ambiguous_members_are_not_reported_as_complete(api, case, status, counts):
    src, routes = _case()
    node = src[2]['graph']['nodes'][0]
    req = _request()
    if case == 'third_missing':
        node['identity']['id'] = '000073'
    elif case == 'third_nonunique':
        src[2]['graph']['nodes'].append({**deepcopy(node), 'id': 'c_extra'})
    elif case == 'third_incomplete':
        node['identity_types'].clear()
    elif case == 'different':
        req = (0, 'abc', ('a', 'a_0'), ('b', 'b_1'))
    else:
        routes[0]['selected_bindings'][0]['key_columns'] = ['other_axis']
    row = api.observed_pair_checks(src, routes, [req])[0, 'abc']
    assert row['status'] == status and row.get('selected_binding_match_counts') == counts
    assert 'first_observation_result' not in row


def test_name_conflict_preserves_independent_bound_sections_and_role_observations(api):
    sources, routes = _case()
    n = sources[0]['graph']['nodes'][0]
    n['supports'].append({**deepcopy(n['supports'][0]), 'label': 'conflicting second name'})
    row = api.observed_pair_checks(sources, routes, [_request()])[0, 'abc']
    assert row['names_recorded_for_all'] is False and row['observed_names_equal'] is None
    assert len(row['shared_bound_section_ids']) == 1
    assert row['relation_and_neighbor_witnesses_equal'] is True
    assert row['applicability_and_row_boundaries_equal'] is None
    assert row['member_observations'][0]['observation_reason'] == 'missing_or_conflicting_names'


def test_verified_shared_identity_reports_ordinary_roles_and_full_facts_separately(api):
    sources, routes = _case()
    sources[1]['graph']['nodes'][1]['identity']['id'] = 'another_peer'
    row = api.observed_pair_checks(sources, routes, [_request()])[0, 'abc']
    assert row['relation_and_neighbor_witnesses_equal'] is False
    assert row['shared_identity_relation_witnesses_equal'] is True
    assert row['first_observation_result'] == 'observations_agree'


def test_same_raw_ids_across_owners_are_resolved_in_their_own_graph(api):
    sources, routes = _case()
    for side in sources:
        side['graph']['nodes'][0]['id'] = 'shared_input_id'
        side['graph']['edges'][0]['source'] = 'shared_input_id'
    req = (0, 'abc', ('a', 'shared_input_id'), ('b', 'shared_input_id'))
    row = api.observed_pair_checks(sources, routes, [req])[0, 'abc']
    assert [(m['contributor_id'], m['input_node_id']) for m in row['member_observations']] == [
        (owner, 'shared_input_id') for owner in ('a', 'b', 'c')]
    assert row['observed_names_equal'] and row['relation_and_neighbor_witnesses_equal']
