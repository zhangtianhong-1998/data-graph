from copy import deepcopy

from lightgraph.construction.fusion import definition_contract
from lightgraph.construction.identity_coverage import identity_evidence_coverage
from lightgraph.contracts import canonical, digest


def contributor(owner, doc, section, *, scope='global', catalog='shared', valid=True):
    source = {'source_id': 'warehouse', 'schema': 'public', 'table': owner}
    columns = ['organization']
    domain = 'private-domain-' + owner
    entity = {'type': 'Area', 'identity_scope': scope, 'identity_domain': domain, 'key_names': ['code'],
              'identity_evidence_refs': ['keyref', 'missingref']}
    receipt = {'binding': digest(domain, tuple(source.values()), columns) if valid else 'wrong',
               'key_sections_v1': [section], 'shared_key_sections_v2': [section],
               'registry_key_claims_v1': [{'section': section, 'issuer': 'publisher', 'catalog': catalog,
                                           'version': 'v1', 'key_roles': ['org']}]}
    record = {'type': 'Area', 'id': 'PRIVATE-ROW-' + owner, 'label': 'PRIVATE-LABEL',
              'identity_domain': domain, 'supports': [{'provenance': {
                'source': source, 'key_columns': columns, 'global_identity_authority': receipt}}]}
    return {'contributor_id': owner, 'artifact_id': owner + '-artifact', 'graph': {
        'definitions': {'entities': [entity]}, 'nodes': [record]}, 'has_evidence': True,
        'evidence_records': {'keyref': {'kind': 'document_section',
            'details': {'document_id': doc, 'revision': 1, 'section_id': 'key', 'complete': True}},
            'version': {'kind': 'document_version', 'details': {'document_id': doc, 'revision': 1}}}}


def test_archived_document_intersection_is_neither_required_nor_sufficient_for_bound_proofs():
    units = [contributor('a', 'doc-a', '1' * 64), contributor('b', 'doc-b', '2' * 64),
             contributor('c', 'doc-a', '1' * 64, valid=False), contributor('d', 'doc-a', '1' * 64, scope='unknown')]
    missing = units[3]
    del missing['evidence_records']
    missing['has_evidence'] = False
    old = deepcopy(units)
    result = identity_evidence_coverage(units, {})
    assert units == old
    assert result == identity_evidence_coverage(units[::-1], {})
    pairs = {tuple(p['contributors']): p for p in result['unit_pairs']}
    ab = pairs['a', 'b']
    assert not ab['observed_document_ids_in_common'] and not ab['intersecting_node_bound_shared_sections']
    assert len(ab['intersecting_node_bound_registry_digests']) == 1
    assert ab['archived_cross_table_proof_route'] == 'shared_proof_present'
    ac = pairs['a', 'c']
    assert ac['observed_document_ids_in_common'] == ['doc-a'] and ac['archived_cross_table_proof_route'] == 'no_shared_proof_observed'
    assert pairs['a', 'd']['observed_document_overlap_status'] == 'unknown'
    a = result['units']['a']
    assert a['documents']['full_visible_inventory'] == 'not_archived'
    assert a['documents']['documents_with_complete_section_read'] == ['doc-a']
    assert a['nodes'] == a['supports'] == a['supports_with_key_sections_v1'] == a['distinct_key_sections_v1'] == 1
    assert a['nodes_with_shared_section_proof'] == a['nodes_with_registry_proof'] == 1
    assert a['identity_evidence_ref_kinds'] == {'document_section': 1, 'unresolved': 1}
    assert result['units']['c']['supports_with_key_sections_v1'] == 1
    assert result['units']['c']['supports_with_bound_shared_sections'] == 0
    d = result['units']['d']
    assert d['definitions_by_scope'] == {'unknown': 1} and d['nodes_with_registry_proof'] == 0
    assert d['documents']['evidence_archive'] == 'not_archived' and d['supports_with_bound_registry'] == 1
    assert all(private not in canonical(result) for private in ('PRIVATE-LABEL', 'PRIVATE-ROW', 'private-domain'))


def test_variant_contracts_are_linked_to_output_definitions_and_actual_counts():
    units = [contributor('a', 'd1', '1' * 64), contributor('b', 'd2', '2' * 64)]
    outputs = []
    for item in units:
        original, = item['graph']['definitions']['entities']
        contract_digest = digest(definition_contract(original))
        outputs.append(dict(original, original_type='Area', type='Area__def_' + contract_digest[:32],
                            definition_variant={'contract_digest': contract_digest}))
    graph = {'definitions': {'entities': outputs}, 'nodes': [{'type': outputs[0]['type']}] * 3,
             'candidate_identity_merges': [{}]}
    report = identity_evidence_coverage(units, graph)
    assert report['committed'] == {'node_count': 3, 'candidate_groups': 1}
    family, = report['variant_families']
    assert family['family'] == 'Area' and family['committed_node_count'] == 3
    for n, row in enumerate(family['input_contracts']):
        assert row['matching_committed_types'] == [outputs[n]['type']]
        assert row['node_count'] == 1
    assert sorted(row['node_count'] for row in family['committed_definitions']) == [0, 3]


def test_old_field_locator_and_empty_archive_are_reported_without_granting_identity():
    unit = contributor('a', 'doc-a', '3' * 64)
    unit['evidence_records'] = {}
    marker = unit['graph']['nodes'][0]['supports'][0]['provenance']['global_identity_authority']
    del marker['shared_key_sections_v2']
    second = deepcopy(unit)
    second['contributor_id'] = 'b'
    del second['has_evidence']
    report = identity_evidence_coverage([unit, second], {})
    a = report['units']['a']
    assert a['supports_with_key_sections_v1'] == 1 and a['nodes_with_registry_proof'] == 0
    assert a['documents']['evidence_archive'] == 'present'
    assert report['units']['b']['documents']['evidence_archive'] == 'unknown'
    assert report['unit_pairs'][0]['archived_cross_table_proof_route'] == 'no_shared_proof_observed'
    assert report['committed']['candidate_groups'] is None


def test_shared_registry_and_cross_table_access_are_independent():
    units = [contributor(owner, 'doc-' + owner, digest(owner)) for owner in 'abcd']
    for item, fingerprint in zip(units[:3], ['PRIVATE-GRANT-1', 'PRIVATE-GRANT-1', 'PRIVATE-GRANT-2'], strict=True):
        item['graph']['access_fingerprint'] = fingerprint
    result = identity_evidence_coverage(units, {})
    pairs = {tuple(p['contributors']): p for p in result['unit_pairs']}
    assert all(p['archived_cross_table_proof_route'] == 'shared_proof_present' for p in pairs.values())
    assert {owner: pairs['a', owner]['cross_table_access_fingerprint_status'] for owner in 'bcd'} == {
        'b': 'matched', 'c': 'different', 'd': 'missing'}
    assert result['same_physical_binding_grant_exception'] == result['entity_pair_outcomes'] == 'not_evaluated'
    assert 'PRIVATE-GRANT' not in canonical(result)
