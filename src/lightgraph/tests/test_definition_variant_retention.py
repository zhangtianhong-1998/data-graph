"""Definition disagreement is local uncertainty, never a reason to lose valid branches."""
from copy import deepcopy

from lightgraph.construction.fusion import relation_identity
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.construction.workspace import _merge_graphs


def branch(*, label_kind='identifier', relation_acyclic=False):
    return {
        'format_version': 1, 'target': 'graph', 'status': 'published',
        'support_visibility_version': 1,
        'definitions': {'entities': [
            {'type': 'Item', 'key_names': ['code'], 'label': {'type': label_kind}},
            {'type': 'Shared', 'key_names': ['code']}], 'relations': [
                {'type': 'contains', 'source_type': 'Item', 'target_type': 'Shared',
                 'acyclic': relation_acyclic}]},
        'nodes': [
            {'id': 'item', 'type': 'Item', 'identity': {'code': '1'},
             'supports': [{'scope_ref': 'public', 'label': '指标'}]},
            {'id': 'shared', 'type': 'Shared', 'identity': {'code': 'common'},
             'supports': [{'scope_ref': 'public', 'label': '共同节点'}]}],
        'edges': [{'id': 'relation', 'type': 'contains', 'source': 'item', 'target': 'shared',
                   'supports': [{'scope_ref': 'public'}]}],
        'evidence_refs': [],
    }


def contributors(a, b, *, kind='unit'):
    return [{'kind': kind, 'contributor_id': 'a', 'graph': a},
            {'kind': kind, 'contributor_id': 'b', 'graph': b}]


def test_incompatible_label_contracts_keep_both_branches_and_merge_compatible_types():
    a, b = branch(), branch(label_kind='derived')
    original = deepcopy([a, b])
    inputs = contributors(a, b)
    graph = merge_construction_graphs(inputs, merge_policy={'shared_entity_types': ['Item', 'Shared']})
    reverse = merge_construction_graphs(list(reversed(inputs)), merge_policy={'shared_entity_types': ['Item', 'Shared']})
    assert graph == reverse
    assert [a, b] == original
    assert len(graph['nodes']) == 3
    assert len(graph['edges']) == 2
    assert len([d for d in graph['definitions']['entities'] if d['type'] == 'Shared']) == 1
    item_types = {n['type'] for n in graph['nodes'] if n.get('original_type') == 'Item'}
    assert len(item_types) == 2
    by_node = {n['id']: n for n in graph['nodes']}
    by_relation = {d['type']: d for d in graph['definitions']['relations']}
    for edge in graph['edges']:
        assert by_relation[edge['type']]['source_type'] == by_node[edge['source']]['type']
        assert by_relation[edge['type']]['target_type'] == by_node[edge['target']]['type']
    assert graph['conflict_isolation']['coverage'] == 'complete'
    assert {d['category'] for d in graph['definition_conflicts']} == {'entities', 'relations'}
    assert all(d['status'] == 'unresolved' for d in graph['definition_conflicts'])


def test_variant_ids_survive_nested_merge_and_new_matching_contribution():
    merged = merge_construction_graphs(contributors(branch(), branch(label_kind='derived')))
    again = merge_construction_graphs([{'kind': 'merged', 'contributor_id': 'previous', 'graph': merged}])
    assert [(n['id'], n['type']) for n in again['nodes']] == [(n['id'], n['type']) for n in merged['nodes']]
    assert [(e['id'], e['type']) for e in again['edges']] == [(e['id'], e['type']) for e in merged['edges']]
    assert again['definition_conflicts'] == merged['definition_conflicts']
    extended = merge_construction_graphs([
        {'kind': 'merged', 'contributor_id': 'previous', 'graph': merged},
        {'kind': 'unit', 'contributor_id': 'c', 'graph': branch()}])
    assert {d['type'] for d in extended['definitions']['entities']} == {d['type'] for d in merged['definitions']['entities']}
    assert len(extended['definition_conflicts']) == len(merged['definition_conflicts'])


def test_workspace_publication_preserves_definition_variants_and_unchanged_inputs():
    a, b = branch(), branch(label_kind='derived')
    original = deepcopy([a, b])
    graph = _merge_graphs([a, b])
    assert [a, b] == original
    assert len(graph['nodes']) == 3
    assert len(graph['edges']) == 2
    assert len(graph['definition_conflicts']) == 2
    repeated = _merge_graphs([graph])
    assert repeated['nodes'] == graph['nodes']
    assert repeated['edges'] == graph['edges']
    assert repeated['definition_conflicts'] == graph['definition_conflicts']


def test_relation_constraints_are_preserved_as_independent_variants():
    a, b = branch(), branch(relation_acyclic=True)
    graph = _merge_graphs([a, b])
    assert len(graph['nodes']) == 2
    assert len(graph['edges']) == 2
    assert {d['acyclic'] for d in graph['definitions']['relations']} == {True, False}
    assert graph['definition_conflicts'][0]['category'] == 'relations'


def test_global_domain_evidence_is_retained_without_false_definition_conflict():
    a, b = branch(), branch()
    for graph, ref in ((a, 'e1'), (b, 'e2')):
        definition = graph['definitions']['entities'][0]
        definition.update(identity_scope='global', identity_domain='financial-items',
                          identity_context_keys=[], identity_evidence_refs=[ref])
        graph['nodes'][0]['identity_domain'] = 'financial-items'
        graph['evidence_refs'] = [ref]
    graph = merge_construction_graphs(contributors(a, b))
    items = [node for node in graph['nodes'] if node['type'] == 'Item']
    assert len(items) == 1
    definition = next(d for d in graph['definitions']['entities'] if d['type'] == 'Item')
    assert definition['identity_evidence_refs'] == ['unit:a:e1', 'unit:b:e2']
    assert not graph['definition_conflicts']


def test_legacy_default_context_fields_do_not_manufacture_conflicts():
    a, b = branch(), branch()
    b['definitions']['entities'][0].update(identity_scope='unknown', identity_context_keys=[],
                                          identity_domain=None, identity_evidence_refs=[])
    assert not _merge_graphs([a, b])['definition_conflicts']


def test_global_and_project_local_identity_contracts_stay_separate():
    a, b = branch(), branch()
    a['definitions']['entities'][0].update(identity_scope='global', identity_domain='items',
                                          identity_evidence_refs=['e1'])
    b['definitions']['entities'][0].update(identity_scope='scoped', identity_context_keys=['code'])
    graph = _merge_graphs([a, b])
    assert len(graph['nodes']) == 3
    assert len(graph['definition_conflicts']) == 2


def test_incompatible_base_definitions_with_overlapping_old_ids_stay_visible():
    a, b = branch(), branch(label_kind='derived')
    for graph in (a, b):
        graph['definitions']['entities'] = graph['definitions']['entities'][:1]
        graph['definitions']['relations'] = []
        graph['nodes'] = graph['nodes'][:1]
        graph['edges'] = []
    graph = merge_construction_graphs(contributors(a, b, kind='base'))
    assert len(graph['nodes']) == 2
    assert graph['definition_conflicts'][0]['node_ids'] == sorted(n['id'] for n in graph['nodes'])


def test_variant_edge_rekey_preserves_distinct_business_contexts():
    a, b = branch(), branch(label_kind='derived')
    for graph in (a, b):
        edges = []
        for project in ('M', 'K'):
            edge = deepcopy(graph['edges'][0])
            edge['business_context'] = {'version': 1, 'status': 'scoped', 'dimensions': {'project': project},
                                        'value_types': {'project': 'text'}}
            edge['supports'][0]['business_context'] = deepcopy(edge['business_context'])
            edge['id'] = relation_identity(edge)
            edges.append(edge)
        graph['edges'] = edges
    graph = merge_construction_graphs(contributors(a, b), merge_policy={'shared_entity_types': ['Shared']})
    assert len(graph['edges']) == 4
    assert len({e['id'] for e in graph['edges']}) == 4


def test_ambiguous_definition_lookup_stays_pending_without_blocking_other_edges():
    a, b = branch(), branch(label_kind='derived')
    for graph in (a, b):
        graph['definitions']['entities'] = graph['definitions']['entities'][:1]
        graph['definitions']['relations'] = []
        graph['nodes'] = graph['nodes'][:1]
        graph['edges'] = []
    local = branch()
    local['extends_base_graph'] = True
    local['definitions']['entities'] = local['definitions']['entities'][1:]
    local['nodes'] = local['nodes'][1:]
    local['edges'][0]['supports'][0]['lookup_matches'] = [
        {'endpoint': 'source', 'inherited_node_id': 'item'}]
    graph = merge_construction_graphs(contributors(a, b, kind='base') + [
        {'kind': 'unit', 'contributor_id': 'delta', 'graph': local}])
    assert len(graph['nodes']) == 3
    assert graph['edges'] == []
    assert len(graph['unresolved_relations']) == 1
    pending = graph['unresolved_relations'][0]
    assert pending['reason'] == 'definition_endpoint_ambiguous'
    assert len(pending['candidate_endpoints']['source']) == 2
    assert len(pending['candidate_endpoints']['target']) == 1
    assert pending['supports'][0]['lookup_matches'][0]['inherited_node_id'] == 'item'
    assert set(pending['node_ids']) == {node['id'] for node in graph['nodes']}
    again = merge_construction_graphs([{'kind': 'merged', 'contributor_id': 'result', 'graph': graph}])
    assert again['unresolved_relations'][0]['id'] == pending['id']
    assert again['unresolved_relations'][0]['candidate_endpoints'] == pending['candidate_endpoints']
    assert _merge_graphs([graph])['unresolved_relations'] == graph['unresolved_relations']


def test_contextual_relationships_do_not_merge_even_with_legacy_equal_edge_ids():
    a, b = branch(), branch()
    for graph, project in ((a, 'M'), (b, 'K')):
        context = {'version': 1, 'status': 'scoped', 'dimensions': {'project': project},
                   'value_types': {'project': 'text'}}
        graph['edges'][0]['business_context'] = context
        graph['edges'][0]['supports'][0]['business_context'] = deepcopy(context)
    assert len(_merge_graphs([a, b])['edges']) == 2
    graph = merge_construction_graphs(contributors(a, b), merge_policy={'shared_entity_types': ['Item', 'Shared']})
    assert len(graph['nodes']) == 2
    assert len(graph['edges']) == 2


def test_verified_global_identity_keeps_label_and_property_variants_by_default():
    import pytest
    from lightgraph.errors import LightGraphError
    a, b = branch(), branch()
    for graph, label, value in ((a, '主名称', 'M'), (b, '别名', 'K')):
        graph['definitions']['entities'][0].update(identity_scope='global', identity_domain='items',
                                                  identity_evidence_refs=['e1'])
        graph['nodes'][0]['identity_domain'] = 'items'
        graph['nodes'][0]['supports'][0].update(label=label, properties={'project': [value]})
        graph['nodes'][0]['properties'] = {'project': [value]}
    graph = merge_construction_graphs(contributors(a, b))
    node = next(n for n in graph['nodes'] if n['type'] == 'Item')
    assert node['label_conflict'] and set(node['label_variants']) == {'主名称', '别名'}
    assert len(node['supports']) == 2
    assert set(node['properties']['project']) == {'M', 'K'}
    assert {s['properties']['project'][0] for s in node['supports']} == {'M', 'K'}
    published = _merge_graphs([a, b])
    assert next(n for n in published['nodes'] if n['type'] == 'Item')['label_conflict']
    assert _merge_graphs([published])['label_conflict_policy'] == 'retain_variants'
    with pytest.raises(LightGraphError, match='published_graph_identity_conflict'):
        merge_construction_graphs(contributors(a, b), merge_policy={'conflict_mode': 'strict'})
    legacy = merge_construction_graphs(contributors(a, b), merge_policy={'conflict_mode': 'quarantine'})
    assert not any(n['type'] == 'Item' for n in legacy['nodes'])


def test_existing_source_domain_is_not_mistaken_for_an_already_applied_fusion_rule():
    import pytest
    from lightgraph.construction.fusion import prepare_graph
    from lightgraph.errors import LightGraphError
    policy = {'entity_rules': [{'entity_type': 'Item', 'canonical_type': 'CanonicalItem',
                               'identity_domain': 'items', 'key_mapping': {'id': 'code'}}]}
    source = branch()
    source['definitions']['entities'][0]['identity_domain'] = 'items'
    source['nodes'][0]['identity_domain'] = 'items'
    prepared = prepare_graph(source, policy)
    node = next(n for n in prepared['nodes'] if n['type'] == 'CanonicalItem')
    assert node['identity'] == {'id': '1'}
    assert prepare_graph(prepared, policy)['nodes'] == prepared['nodes']
    source['definitions']['entities'][0]['identity_domain'] = 'different-domain'
    with pytest.raises(LightGraphError, match='fusion_identity_domain_conflict'):
        prepare_graph(source, policy)


def test_late_definition_conflict_has_same_ids_as_direct_merge():
    a, b = branch(), branch(label_kind='derived')
    direct = merge_construction_graphs(contributors(a, b))
    first = merge_construction_graphs(contributors(a, b)[:1])
    staged = merge_construction_graphs([
        {'kind': 'merged', 'contributor_id': 'previous', 'graph': first},
        contributors(a, b)[1]])
    assert [(n['id'], n['type']) for n in direct['nodes']] == [(n['id'], n['type']) for n in staged['nodes']]
    assert [(e['id'], e['type']) for e in direct['edges']] == [(e['id'], e['type']) for e in staged['edges']]
    assert direct['definition_conflicts'] == staged['definition_conflicts']


def test_generated_variant_name_cannot_collide_with_an_original_type():
    from lightgraph.construction.fusion import definition_contract
    from lightgraph.contracts import digest
    a, b = branch(), branch(label_kind='derived')
    signature = digest(definition_contract(a['definitions']['entities'][0]))
    occupied = 'Item__def_' + signature[:32]
    c = branch()
    c['definitions']['entities'] = [{'type': occupied, 'key_names': ['code']}]
    c['definitions']['relations'] = []
    c['nodes'] = [deepcopy(c['nodes'][0])]
    c['nodes'][0]['type'] = occupied
    c['edges'] = []
    graph = merge_construction_graphs(contributors(a, b) + [{'contributor_id': 'c', 'graph': c}])
    assert len(graph['definitions']['entities']) == 4
    originals = [d for d in graph['definitions']['entities'] if d['type'] == occupied]
    assert len(originals) == 1 and 'original_type' not in originals[0]
    assert len([d for d in graph['definitions']['entities'] if d.get('original_type') == 'Item']) == 2


def test_late_original_type_name_collision_preserves_existing_variant_node_ids():
    a, b = branch(), branch(label_kind='derived')
    first = merge_construction_graphs(contributors(a, b))
    name = next(d['type'] for d in first['definitions']['entities'] if d.get('original_type') == 'Item')
    extra = branch()
    extra['definitions']['entities'] = [{'type': name, 'key_names': ['code']}]
    extra['definitions']['relations'] = []
    extra['nodes'] = [extra['nodes'][0]]
    extra['nodes'][0]['type'] = name
    extra['edges'] = []
    result = merge_construction_graphs([
        {'kind': 'merged', 'contributor_id': 'previous', 'graph': first},
        {'kind': 'unit', 'contributor_id': 'new', 'graph': extra}])
    assert {n['id'] for n in first['nodes']} <= {n['id'] for n in result['nodes']}
    assert len({d['type'] for d in result['definitions']['entities']}) == 4


def test_context_disjoint_relationships_do_not_create_a_false_cross_draft_cycle():
    a, b = branch(relation_acyclic=True), branch(relation_acyclic=True)
    for graph, project in ((a, 'M'), (b, 'K')):
        graph['definitions']['entities'] = graph['definitions']['entities'][:1]
        graph['definitions']['relations'][0]['target_type'] = 'Item'
        graph['nodes'][1]['type'] = 'Item'
        context = {'version': 1, 'status': 'scoped', 'dimensions': {'project': project},
                   'value_types': {'project': 'text'}}
        graph['edges'][0]['business_context'] = context
        graph['edges'][0]['supports'][0]['business_context'] = deepcopy(context)
    b['edges'][0].update(source='shared', target='item')
    assert len(_merge_graphs([a, b])['edges']) == 2
    graph = merge_construction_graphs(contributors(a, b), merge_policy={'shared_entity_types': ['Item']})
    assert len(graph['edges']) == 2
    assert graph['conflict_isolation']['edges'] == 0
