"""Optional tuple evidence yields to complete raw relations under a working budget."""
from copy import deepcopy

from lightgraph.construction.accumulation import GraphAccumulator
from lightgraph.construction.executor import execute_draft
from lightgraph.contracts import canonical
from lightgraph.sources import MemorySource
from lightgraph.tests.test_construction_endpoint_lookup import example


def _lookup_graph(limit, *, witnesses=True, relations=True):
    orders, customers, draft = example()
    if not relations:
        draft['relations'] = []
    with MemorySource([orders], {orders.qualified_name: [(f'O{i}', f'e{i}', 'public') for i in range(4)]}) as a, \
            MemorySource([customers], {customers.qualified_name: [(f'C{i}', f'e{i}', f'Name{i}', 'public') for i in range(4)]}) as b:
        if witnesses:
            # Exercise the optional-witness budget with controlled source identities.
            a._observation_source_digest = 'a' * 64
            b._observation_source_digest = 'b' * 64
        return execute_draft(draft, [a, b], [orders, customers], max_working_bytes=limit)


def test_nested_lookup_witnesses_yield_without_losing_raw_relations():
    baseline = _lookup_graph(31000, witnesses=False)
    full = _lookup_graph(100000)
    result = _lookup_graph(31000)
    assert baseline['passed'] and result['passed'], result['diagnostics']
    assert len(full['graph']['edges']) == len(result['graph']['edges']) == 4
    assert result['graph'] == baseline['graph']
    assert result['evidence_diagnostics'] == [{'code': 'optional_relation_witnesses_dropped',
        'reason': 'memory_budget', 'original_relations_retained': True}]
    for edge in full['graph']['edges']:
        support, = edge['supports']
        assert support['source_projection_witness'] and support['lookup_matches'][0]['source_projection_witness']


def test_dropped_evidence_does_not_claim_retained_relations_after_budget_failure():
    result = _lookup_graph(30000)
    assert not result['passed'] and result['graph']['edges'] == []
    assert result['evidence_diagnostics'][0]['original_relations_retained'] is False
    assert not _lookup_graph(10000, witnesses=False)['evidence_diagnostics']
    assert not _lookup_graph(10000, relations=False)['evidence_diagnostics']


def test_nested_removal_reindexes_and_deduplicates_without_losing_lookup_values():
    item = {'id': 'edge', 'supports': []}
    measured = [0, 0]
    def account(payload, index):
        measured[0] += payload
        measured[1] += index
    acc = GraphAccumulator(account)
    acc.register(item)
    support = {'scope_ref': 'public', 'lookup_matches': [
        {'resolved_key': ['actual'], 'source_projection_witness': {'tuple': 'a' * 64}}]}
    acc.append(item, 'supports', deepcopy(support))
    support['lookup_matches'][0]['source_projection_witness']['tuple'] = 'b' * 64
    acc.append(item, 'supports', deepcopy(support))
    saved, freed = acc.drop_support_field([item], 'source_projection_witness', nested_list='lookup_matches')
    measured[0] -= saved
    measured[1] -= freed
    assert saved > 0 and freed == 128
    assert measured[0] == len(canonical(item).encode())
    assert item['supports'] == [{'scope_ref': 'public', 'lookup_matches': [{'resolved_key': ['actual']}]}]
    before = measured[:]
    acc.append(item, 'supports', deepcopy(item['supports'][0]))
    assert measured == before


def _inherited_graph(limit):
    orders, _, draft = example()
    draft['entities'] = draft['entities'][:1]
    draft.update(extends_base_graph=True, inherited_entity_types=['Customer'])
    draft['relations'][0]['mappings'][0]['target_lookup'].update(
        source_id='__base_graph__', table='Customer', match_columns=['id'], key_columns=['id'])
    base = {'definitions': {'entities': [{'type': 'Customer', 'key_names': ['id']}]},
            'nodes': [{'id': f'customer-{i}', 'type': 'Customer', 'identity': {'id': f'C{i}'},
                       'supports': [{'scope_ref': 'public', 'business_context': {'status': 'global', 'dimensions': {}},
                                     'provenance': {'source': {'source_id': 'base', 'schema': 'public', 'table': 'customers'}}}]}
                      for i in range(4)]}
    with MemorySource([orders], {orders.qualified_name: [(f'O{i}', f'C{i}', 'public') for i in range(4)]}) as source:
        return execute_draft(draft, [source], [orders], base_graph=base, max_working_bytes=limit)


def test_inherited_hash_evidence_yields_but_endpoint_and_permission_remain(monkeypatch):
    from lightgraph.construction.inherited_endpoints import InheritedEndpoints
    original = InheritedEndpoints.resolve
    def no_optional_witness(self, *args, **kwargs):
        return original(self, *args, **{**kwargs, 'collect_witness': False})
    monkeypatch.setattr(InheritedEndpoints, 'resolve', no_optional_witness)
    baseline = _inherited_graph(100000)
    limit = baseline['metrics']['working_memory_estimated_peak_bytes']
    assert baseline['passed'] and len(baseline['graph']['edges']) == 4
    monkeypatch.setattr(InheritedEndpoints, 'resolve', original)
    result = _inherited_graph(limit)
    assert result['passed'], result['diagnostics']
    assert result['graph'] == baseline['graph']
    assert result['evidence_diagnostics'][0]['original_relations_retained'] is True
    for edge in result['graph']['edges']:
        match, = edge['supports'][0]['lookup_matches']
        assert match['inherited_node_id'] and match['scope_ref'] == 'public' and match['provenance']
        assert 'inherited_observation_witness' not in match


def test_inherited_digest_matches_canonical_and_is_skipped_after_index_budget_degrades():
    from lightgraph.construction.inherited_endpoints import InheritedEndpoints
    from lightgraph.contracts import digest
    base = {'definitions': {'entities': [{'type': 'Item', 'key_names': ['id']}]},
            'nodes': [{'id': 'item', 'type': 'Item', 'identity': {'id': 'A'},
                       'supports': [{'scope_ref': 'public', 'properties': {'amount': [1, True, 1.0]},
                                     'business_context': {'status': 'global', 'dimensions': {}}}]}]}
    inherited = InheritedEndpoints(base)
    enabled = True
    def degrade(_):
        nonlocal enabled
        enabled = False
    _, match = inherited.resolve('Item', None, ['id'], ['A'], 'target', clock=lambda: None,
                                  charge=degrade, collect_witness=lambda: enabled)[0]
    assert inherited._graph_digest is None and 'inherited_observation_witness' not in match
    _, match = inherited.resolve('Item', None, ['id'], ['A'], 'target', clock=lambda: None,
                                  charge=lambda _: None, collect_witness=True)[0]
    assert match['inherited_observation_witness']['graph_digest'] == digest(base)
    assert match['inherited_observation_witness']['support_digest'] == digest(base['nodes'][0]['supports'][0])


def _projection_support():
    from lightgraph.tests.test_fusion_census_edges import _verified_edge
    edge = _verified_edge('edge', 'contains_area', [[2, 3]])
    support = edge['supports'][0]
    del support['source_row_witness']
    del support['provenance']['source_column_ordinals']
    support['provenance']['columns'] = ['AREA_L1_ID', 'AREA_L2_ID']
    support['source_projection_witness'] = {
        'kind': 'source_projection_v1', 'basis': 'complete_distinct_projection', 'complete': True,
        'source_binding': 'a' * 64, 'projection': 'b' * 64, 'tuple': 'c' * 64,
        'columns': ['AREA_L1_ID', 'AREA_L2_ID']}
    return edge, support


def test_projection_witness_must_cover_every_mapped_column_without_mutating_raw_edge():
    from lightgraph.construction.relation_assertions import assertion_signature, support_assertion_checks
    for field in ('scope_columns', 'context_columns', 'property_columns', 'business_context_mapping'):
        edge, support = _projection_support()
        if field == 'property_columns':
            support['provenance'][field] = {'tenant': 'tenant'}
            support['properties']['tenant'] = ['A']
        elif field == 'business_context_mapping':
            support['provenance'][field] = {'status': 'scoped', 'columns': {'project': 'tenant'}}
            support['business_context'] = {'status': 'scoped', 'dimensions': {'project': 'A'}}
        else:
            support['provenance'][field] = ['tenant']
            support['row_context'] = {'values': {'tenant': 'A'}, 'value_types': {'tenant': 'text'}}
        before = deepcopy(edge)
        assert 'missing_or_invalid_projection_columns' in support_assertion_checks(support)['blockers']
        assert assertion_signature(edge, {'type': edge['type']})[0] == 'missing_or_invalid_projection_columns'
        assert edge == before
        support['source_projection_witness']['columns'].append('tenant')
        if field != 'scope_columns':
            # Provenance lists the actual projection too, independently of the witness.
            assert 'missing_or_invalid_projection_columns' in support_assertion_checks(support)['blockers']
            support['provenance']['columns'].append('tenant')
        assert not support_assertion_checks(support)['blockers']


def test_malformed_column_metadata_produces_blockers_instead_of_type_errors():
    from lightgraph.construction.relation_assertions import support_assertion_checks
    for field in ('columns', 'scope_columns', 'context_columns', 'property_columns', 'business_context_mapping'):
        _, support = _projection_support()
        if field == 'business_context_mapping':
            support['provenance'][field]['columns'] = {'project': ['tenant']}
        elif field == 'property_columns':
            support['provenance'][field] = {'tenant': ['tenant']}
        else:
            support['provenance'][field] = [{'bad': 'column'}]
        assert 'missing_or_invalid_projection_columns' in support_assertion_checks(support)['blockers']


def test_lookup_witness_presence_never_means_verified_fact_equivalence():
    from lightgraph.construction.relation_assertions import support_assertion_checks
    _, support = _projection_support()
    support['lookup_matches'] = [{'source_projection_witness': deepcopy(support['source_projection_witness'])}]
    checks = support_assertion_checks(support)
    by_name = {item['check']: item for item in checks['checks']}
    assert by_name['lookup_observation_witness_presence']['status'] == 'passed'
    assert by_name['lookup_fact_equivalence']['status'] == 'not_checked'
    assert 'lookup_fact_equivalence_unverified' in checks['blockers']
