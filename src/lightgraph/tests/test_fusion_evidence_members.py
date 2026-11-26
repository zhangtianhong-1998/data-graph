from copy import deepcopy
from pathlib import Path
from runpy import run_path

from lightgraph.contracts import digest
from lightgraph.tests.test_batch_overlap_patterns import unit

collect = run_path(str(Path(__file__).resolve().parents[3] / 'scripts/fusion_evidence_members.py'))[
    'collect_member_evidence']


def test_bound_document_locators_do_not_imply_all_supports_have_same_authority():
    c = unit('a', 'Region', 'code', field='tax_caliber')
    graph = c['graph']
    node = graph['nodes'][0]
    support, = node['supports']
    doc = {'document_id': 'catalog', 'revision': 8, 'section_id': 'city', 'content_digest': digest('v8')}
    section = digest('identity_field_section_v1', *doc.values())
    support['evidence_refs'] = ['probe', 'doc', 'missing']
    prov = support['provenance']
    prov['identity_qualifiers'] = {'tax_caliber': 'caliber'}
    marker = {'binding': digest('catalog', ('db', 'dm', 'items'), ['code']), 'key_sections_v1': [section]}
    prov['global_identity_authority'] = marker
    c['evidence_records'] = {
        'doc': {'kind': 'document_section', 'details': {**doc, 'complete': True, 'body': 'secret-text'}},
        'probe': {'kind': 'source_probe', 'source_ref': {**prov['source'], 'dsn': 'secret-dsn'},
                  'details': {'operation': 'get_metadata', 'complete': True, 'sql': 'secret-sql'}}}
    member = collect(c, ['a_0'])['a_0']
    assert member['global_scope_authority_sections'] == [section]
    assert member['typed_key'] == {'value_type': 'text', 'value': '000071'}
    assert member['bindings'][0]['access']['status'] == 'unverified'
    locator = {r['ref']: r for r in member['evidence_locators']}
    assert locator['doc']['versioned_section_id'] == section
    assert locator['probe']['source'] == prov['source']
    assert locator['missing']['record_available'] is False
    assert 'secret-' not in str(member)
    proof, = member['support_groups']
    assert proof['qualified_name'] == node['label'] and proof['qualifiers'][0]['role'] == 'caliber'
    assert proof['qualifiers'][0]['values'] == ['tax_caliber_private_value']
    second = deepcopy(support)
    second['provenance'].pop('label_column')
    second['provenance']['global_identity_authority']['binding'] = digest('wrong')
    node['supports'].append(second)
    observed = collect(c, ['a_0'])['a_0']
    assert not observed['common_bound_sections']
    assert {row['qualified_name'] for row in observed['support_groups']} == {None, node['label']}
    assert {row['authority']['binding_matches'] for row in observed['support_groups']} == {True, False}


def test_incident_edges_counted_once_per_direction_and_examples_are_bounded():
    c = unit('a', 'Region', 'code')
    graph = c['graph']
    edge, = graph['edges']
    edge['supports'] *= 2
    graph['edges'].extend([{**edge, 'id': 'e2'}, {**edge, 'id': 'loop', 'target': 'a_0'}])
    graph['nodes'].append(deepcopy(graph['nodes'][1]))
    original = deepcopy(c)
    result = collect(c, ['a_0', 'a_1', 'invalid'], example_limit=1)
    assert result['invalid']['status'] == 'missing' and result['invalid']['matches'] == 0
    assert result['a_1']['status'] == 'duplicate' and result['a_1']['matches'] == 2
    roles = {r['end']: r for r in result['a_0']['roles']}
    outgoing, incoming = roles['source'], roles['target']
    assert [outgoing[k] for k in ('edge_count', 'support_count', 'self_loop_edges', 'examples_omitted')] == [3, 6, 1, 2]
    assert outgoing['examples'][0]['peer_status'] == 'duplicate'
    assert outgoing['examples'][0]['peer_typed_key'] is None
    assert [incoming[k] for k in ('edge_count', 'self_loop_edges', 'examples_omitted')] == [1, 1, 0]
    assert incoming['key_columns'] == ['parent_code'] and incoming['opposite_key_columns'] == ['code']
    assert incoming['identity_role_semantics'] == 'unverified'
    assert original == c
    graph['edges'].reverse()
    assert collect(c, result.keys(), example_limit=1) == result


def test_table_grant_locates_policy_difference_without_emitting_connection_configuration():
    from lightgraph.construction.projection_access import _CheckpointGrant

    base = unit('a', 'Region', 'code')
    source = {'id': 'db', 'kind': 'gauss_database', 'allowed_tables': ['dm.items'], 'allow_fact_data': True,
              'dsn_env': 'PRIVATE_ENV', 'allowed_columns': {'dm.items': ['code', 'display_name']}}
    policy = {'sources': [source], 'scope_bindings': [], 'isolation_domain': 'workspace1', 'document_scopes': []}
    base['_projection_grant'] = _CheckpointGrant('authorized', policy)
    first = collect(base, ['a_0'])['a_0']['bindings'][0]['access']
    source['allowed_tables'].append('dm.unrelated')
    second = collect(base, ['a_0'])['a_0']['bindings'][0]['access']
    assert first['status'] == 'checkpoint_table_grant' and first['key_columns_covered']
    a, b = first['frozen_policy_component_digests'], second['frozen_policy_component_digests']
    assert {k for k in a if a[k] != b[k]} == {'source_table_selection'}
    assert 'PRIVATE_ENV' not in str(first)
