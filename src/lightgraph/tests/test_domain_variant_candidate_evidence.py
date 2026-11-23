from copy import deepcopy

import pytest

from lightgraph.construction.domain_variant_candidates import domain_variant_candidates


def _graph():
    definitions, nodes = [], []
    for kind, domain in [('a', 'long domain'), ('b', 'short domain')]:
        definitions.append({
            'type': kind, 'original_type': 'Indicator', 'definition_variant': {'version': 1},
            'key_names': ['catalog', 'code'], 'identity_domain': domain, 'identity_scope': 'global',
        })
        for code in ('a', 'b'):
            provenance = {'source': {'source_id': 's', 'schema': 'x', 'table': 'facts'},
                          'key_columns': ['catalog_id', 'metric_id'], 'label_column': 'name'}
            if kind == 'b':
                provenance['predicate'] = {'op': 'is_not_null', 'column': 'metric_id'}
            nodes.append({'id': kind + code, 'type': kind, 'original_type': 'Indicator',
                          'identity': {'catalog': 2, 'code': code},
                          'identity_types': {'catalog': 'integer', 'code': 'text'},
                          'identity_domain': domain,
                          'supports': [{'scope_ref': 'public', 'contributor_id': 'u',
                                        'provenance': provenance, 'label': 'same label'}]})
    return {'nodes': nodes, 'definitions': {'entities': definitions}}


def _pairs(graph):
    return [group['node_ids'] for group in domain_variant_candidates(graph)]


def test_same_label_and_physical_key_bind_only_the_original_typed_values():
    graph = _graph()
    assert _pairs(graph) == [['aa', 'ba'], ['ab', 'bb']]
    graph['nodes'].reverse()
    assert _pairs(graph) == [['aa', 'ba'], ['ab', 'bb']]


@pytest.mark.parametrize('location,updates', [
    ('definition', {'key_names': ['code', 'catalog']}),
    ('definition', {'identity_scope': 'unknown'}),
    ('definition', {'original_type': 'BusinessUnit'}),
    ('node', {'identity_types': {'code': 'text'}}),
    ('node', {'identity_context': {'status': 'scoped', 'dimensions': {'dept': 'A'}}}),
    ('node', {'identity': {'code': 'a', 'catalog': '2'}, 'identity_types': {'code': 'text', 'catalog': 'text'}}),
    ('provenance', {'source': {'source_id': 's', 'schema': 'x', 'table': 'another'}}),
    ('provenance', {'key_columns': ['metric_id', 'catalog_id']}),
    ('provenance', {'label_column': 'english_name'}),
    ('provenance', {'predicate': {'op': 'eq', 'column': 'status', 'value': 'active'}}),
    ('provenance', {'constant_properties': {'role': 'quota'}}),
    ('support', {'contributor_id': 'another_unit'}),
    ('support', {'execution_origins': [{'contributor_id': 'another_unit'}]}),
    ('support', {'identity_origins': [{'id': 'absorbed'}]}),
    ('support', {'inherited_from': {'project': 'other_project'}}),
    ('support', {'row_context': {'values': {'catalog_id': '2'}, 'value_types': {'catalog_id': 'text'}}}),
    ('support', {'business_context': {'status': 'scoped', 'dimensions': {'dept': 'A'}}}),
    ('support', {'required_scope_refs': ['private']}),
])
def test_missing_or_incompatible_physical_evidence_is_not_a_review_candidate(location, updates):
    graph = _graph()
    node = graph['nodes'][2]
    support = node['supports'][0]
    selected = {'definition': graph['definitions']['entities'][1], 'node': node,
                'support': support, 'provenance': support['provenance']}
    selected[location].update(updates)
    assert ['aa', 'ba'] not in _pairs(graph)


def test_one_common_support_cannot_hide_an_additional_incompatible_observation():
    graph = _graph()
    node = graph['nodes'][2]
    support = deepcopy(node['supports'][0])
    support['provenance']['source']['table'] = 'unrelated_table'
    node['supports'].append(support)
    assert _pairs(graph) == [['ab', 'bb']]


def test_untagged_source_is_local_only_in_a_direct_workspace_and_cannot_match_tagged_origins():
    graph = _graph()
    for node in graph['nodes']:
        support = node['supports'][0]
        support.pop('contributor_id')
        support['execution_origins'] = [{'draft_id': node['type']}]
    assert not domain_variant_candidates(graph)
    assert len(domain_variant_candidates(graph, local_workspace=True)) == 2

    graph['nodes'][2]['supports'][0]['contributor_id'] = 'unit1'
    graph['nodes'][3]['supports'][0]['execution_origins'][0]['contributor_id'] = 'unit2'
    assert not domain_variant_candidates(graph, local_workspace=True)
