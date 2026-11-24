"""Review overlapping observed codes in two published domains without equating the domains."""
from collections import defaultdict
from copy import deepcopy

from lightgraph.construction.executor import execute_draft
from lightgraph.construction.workspace import _merge_graphs
from lightgraph.sources import MemorySource
from lightgraph.tests.test_observed_identity import unit
from lightgraph.tests.test_retrieval_identity_and_row_proofs import table
from lightgraph.tests.test_web_graph_review import client
from lightgraph.tests.test_web_publication_identity import GRAPH, ROOT, listing, publish, revoke, submit


def _draft_graphs():
    facts = [('orders', 'order', '订货'), ('income', 'revenue', '收入'),
             ('sheet', 'sheet-only', '工作表科目')]
    tables = [table(name, ['code', 'name', 'record']) for name, _, _ in facts]
    rows = {spec.qualified_name: [(code, name, spec.name)] for spec, (_, code, name) in zip(tables, facts)}
    contributions = []
    for owner, domain, sources in [('tree', '完整指标编码体系', tables), ('tracking', 'catalog.indicator', tables[:2])]:
        bindings = [{'source_id': 's', 'table': spec.name} for spec in sources]
        metric_maps = [{**binding, 'key_columns': ['code'], 'label_column': 'name'} for binding in bindings]
        if owner == 'tree':
            for mapping in metric_maps:
                mapping['predicate'] = {'column': 'code', 'op': 'is_not_null'}
        target = owner + '_record'
        draft = {'draft_id': owner, 'entities': [
            {'type': 'indicator', 'identity_scope': 'global', 'identity_domain': domain,
             'identity_evidence_refs': ['table-probe'], 'key_names': ['indicator_code'], 'mappings': metric_maps},
            {'type': target, 'key_names': ['record'], 'mappings': [
                {**b, 'key_columns': ['record']} for b in bindings]}],
            'relations': [{'type': owner + '_observation', 'source_type': 'indicator', 'target_type': target,
                           'mappings': [{**b, 'source_key_columns': ['code'], 'target_key_columns': ['record']}
                                        for b in bindings]}]}
        with MemorySource(tables, rows) as source:
            result = execute_draft(draft, [source], source.describe())
        assert result['passed'], result['diagnostics']
        contributions.append({**result['graph'], 'draft_id': owner})
    return contributions


def _variant_unit():
    graph = _merge_graphs(_draft_graphs())
    graph.pop('candidate_identity_merges', None)  # A historical unit checkpoint without these candidates.
    return graph


def _by_code(graph):
    members = defaultdict(set)
    for node in graph['nodes']:
        if node.get('original_type', node['type']) == 'indicator':
            members[node['identity']['indicator_code']].add(node['id'])
    return members


def test_frozen_base_contract_is_still_addressable_in_per_code_domain_review():
    from lightgraph.construction.domain_variant_candidates import domain_variant_candidates
    from lightgraph.construction.merger import merge_construction_graphs

    tree, tracking = _draft_graphs()
    # Both revisions were materialized by the same unit in the same project.
    # The source-key witness is scoped to that owner.
    graph = merge_construction_graphs([
        {'kind': 'base', 'contributor_id': 'extract', 'source_namespace': 'project', 'graph': tree},
        {'kind': 'unit', 'contributor_id': 'extract', 'source_namespace': 'project', 'graph': tracking}])
    members = _by_code(graph)
    assert next(n for n in graph['nodes'] if n['identity'] == {'indicator_code': 'sheet-only'})['type'] == 'indicator'
    candidates = domain_variant_candidates(graph)
    assert {frozenset(c['node_ids']) for c in candidates} == {frozenset(members[k]) for k in ('order', 'revenue')}


def test_review_two_codes_in_an_old_publication_and_revoke_individually(client):
    graph = publish(client, [unit('extract', _variant_unit())])
    assert not graph.get('candidate_identity_merges')
    members = _by_code(graph)
    assert {code: len(ids) for code, ids in members.items()} == {'order': 2, 'revenue': 2, 'sheet-only': 1}
    review = listing(client)
    assert review['total_candidates'] == 2
    candidates = {frozenset(c['members']): c for c in review['candidates']}
    assert set(candidates) == {frozenset(members['order']), frozenset(members['revenue'])}
    assert all(c['reason'] == 'same_source_key_different_identity_domains' for c in candidates.values())
    before = client.get(GRAPH + '/export').json()
    decisions = {}
    for code in ('order', 'revenue'):
        response = submit(client, review, candidates[frozenset(members[code])])
        assert response.status_code == 200, response.text
        decisions[code] = response.json()
        review = listing(client)
        assert client.get(GRAPH).json()['total_nodes'] == len(graph['nodes']) - len(decisions)

    after = client.get(GRAPH + '/export').json()
    final_ids = {n['id'] for n in after['entities']}
    assert members['sheet-only'] <= final_ids
    for code in decisions:
        merged, = [n for n in after['entities'] if n['id'] in members[code]]
        assert {o['id'] for o in merged['metadata']['identity_origins']} >= members[code]
        assert len(merged['metadata']['supports']) == 2
    assert len(after['relationships']) == len(before['relationships']) == 5
    originals = {(e['id'], e['source'], e['target']) for e in before['relationships']}
    recovered = {(e['id'], e['source'], e['target']) for e in after['relationships']}
    for edge in after['relationships']:
        for support in edge['metadata']['supports']:
            recovered.update((o['edge_id'], o['source'], o['target']) for o in support.get('endpoint_origins', []))
    assert originals <= recovered
    assert client.post(ROOT + '/retrieval/sync').status_code == 200
    for name, code in [('订货', 'order'), ('收入', 'revenue')]:
        answer = client.post(ROOT + '/retrieval/query', json={'query': name})
        assert answer.status_code == 200, answer.text
        slot, = answer.json()['plan']['conditions']
        assert [c['node_id'] for c in slot['candidates']] == list(members[code] & final_ids)

    undone = revoke(client, review, decisions['order'])
    assert undone.status_code == 200, undone.text
    restored_ids = {n['id'] for n in client.get(GRAPH + '/export').json()['entities']}
    assert members['order'] <= restored_ids and len(restored_ids & members['revenue']) == 1
    undone = revoke(client, listing(client), decisions['revenue'])
    assert undone.status_code == 200, undone.text
    restored = client.get(GRAPH + '/export').json()
    assert {n['id'] for n in restored['entities']} == {n['id'] for n in before['entities']}
    assert {e['id'] for e in restored['relationships']} == {e['id'] for e in before['relationships']}


def test_an_existing_candidate_for_the_same_pair_stays_addressable_once(client):
    graph = _variant_unit()
    order = sorted(_by_code(graph)['order'])
    graph['candidate_identity_merges'] = [{'reason': 'source_key_requires_role_review',
                                          'status': 'candidate_only', 'node_ids': order}]
    published = publish(client, [unit('extract', graph)])
    members = _by_code(published)
    review = listing(client)
    assert review['total_candidates'] == 2
    by_pair = {frozenset(c['members']): c for c in review['candidates']}
    assert by_pair[frozenset(members['order'])]['reason'] == 'source_key_requires_role_review'
    assert by_pair[frozenset(members['revenue'])]['reason'] == 'same_source_key_different_identity_domains'


def test_a_hidden_shared_key_does_not_expose_a_candidate_for_two_visible_objects(client):
    graph = _variant_unit()
    order = next(n for n in graph['nodes'] if n.get('identity_domain') == 'catalog.indicator'
                 and n['identity'] == {'indicator_code': 'order'})
    shared, = order['supports']
    unrelated = deepcopy(shared)
    unrelated['provenance']['source']['table'] = 'independent_catalog'
    order['supports'].append(unrelated)
    shared['scope_ref'] = 'private'
    published = publish(client, [unit('extract', graph)])
    members = _by_code(published)
    visible = {n['id'] for n in client.get(GRAPH + '/export').json()['entities']}
    assert members['order'] <= visible
    candidate, = listing(client)['candidates']
    assert set(candidate['members']) == members['revenue']
