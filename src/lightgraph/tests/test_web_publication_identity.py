"""The actual workbench APIs must review the same immutable graph that readers consume."""
from copy import deepcopy

import pytest

from lightgraph.construction.access import access_fingerprint
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.construction.workflow import publish_premerged_graph
from lightgraph.tests.test_merge_candidate_evidence import candidate_unit
from lightgraph.tests.test_observed_identity import projection, unit
from lightgraph.tests.test_web_graph_review import client, revision, seed


ROOT = '/api/projects/p'
GRAPH = ROOT + '/graph'
REVIEW = GRAPH + '/identity-candidates'
DECIDE = GRAPH + '/identity-decisions'


def publish(client, units):
    config = client.app.state.workspace.result_config('p')
    graph = merge_construction_graphs(units, target_access_fingerprint=access_fingerprint(config))
    publish_premerged_graph(config.output_dir, config, merged=graph, included_units=[], skipped_units=[], unit_attempts={})
    return graph


def listing(client, **params):
    response = client.get(REVIEW, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def submit(client, review, candidate, **changes):
    return client.post(DECIDE, headers=revision(client), json={
        'candidate_id': candidate['candidate_id'], 'verdict': 'same', 'version_id': review['version_id'],
        'revision': review['revision'], **changes})


def revoke(client, review, decision):
    return client.post(DECIDE + '/' + decision['decision_id'] + '/revoke', headers=revision(client),
                       json={'version_id': review['version_id'], 'revision': review['revision']})


def test_real_publication_decision_reaches_export_history_canvas_search_and_can_be_revoked(client):
    seed(client)  # A stale SQLite candidate database must not receive the write.
    units = []
    for name in ('first', 'second'):
        graph = projection('Record', 'code', table=name)
        # Distinct children and two independent relationships survive consolidation of the parents.
        graph['nodes'][1].update(identity={'code': name})
        graph['nodes'][1]['supports'][0]['label'] = name
        units.append(unit(name, graph))
    publish(client, units)
    before = client.get(GRAPH, params={'include_candidates': True}).json()
    assert before['total_nodes'] == 4 and before['total_edges'] == 2
    review = listing(client, version=before['version_id'])
    candidate, = review['candidates']
    assert candidate['status'] == 'candidate' and candidate['channels'] and candidate['supports']
    assert before['alignment_candidates'][0]['candidate_id'] == candidate['candidate_id']
    assert set(candidate['members']) == {n['id'] for n in candidate['nodes']}
    assert {s['provenance']['source']['table'] for s in candidate['supports']} == {'first', 'second'}
    assert client.post(ROOT + '/retrieval/sync').status_code == 200
    response = submit(client, review, candidate)
    assert response.status_code == 200, response.text
    decision = response.json()
    after = client.get(GRAPH).json()
    assert (after['total_nodes'], after['total_edges'], after['pending_alignment_count']) == (3, 2, 0)
    assert after['version_id'] == decision['version_id'] != before['version_id']
    assert client.get(GRAPH, params={'version': before['version_id']}).json()['total_nodes'] == 4
    assert submit(client, review, candidate).status_code == 409
    assert client.get(REVIEW, params={'version': before['version_id']}).status_code == 409
    export = client.get(GRAPH + '/export').json()
    merged, = [n for n in export['entities'] if n['id'] in candidate['members']]
    assert {o['id'] for o in merged['metadata']['identity_origins']} >= set(candidate['members'])
    assert {e['source'] for e in export['relationships']} == {merged['id']}
    assert any(s.get('endpoint_origins') for e in export['relationships'] for s in e['metadata']['supports'])
    query = {'mode': 'search', 'query': '代垫款', 'method': 'keyword', 'hops': 0}
    stale = client.post(ROOT + '/retrieval/query', json=query)
    assert stale.status_code == 409 and stale.json()['error']['code'] == 'retrieval_index_stale'
    assert client.post(ROOT + '/retrieval/sync').status_code == 200
    hit = client.post(ROOT + '/retrieval/query', json=query)
    assert hit.status_code == 200, hit.text
    assert hit.json()['version_id'] == after['version_id']
    outdated = client.post(ROOT + '/retrieval/query', json=query, params={'version': before['version_id']})
    assert outdated.status_code == 409
    assert merged['id'] in hit.text and (set(candidate['members']) - {merged['id']}).isdisjoint(
        node['id'] for node in hit.json()['nodes'])
    reviewed = listing(client)
    assert reviewed['candidates'][0]['has_active_decision'] and reviewed['decisions'][0]['active']
    assert reviewed['candidates'][0]['members'] == candidate['members']
    restored = revoke(client, reviewed, decision)
    assert restored.status_code == 200, restored.text
    assert client.get(GRAPH).json()['total_nodes'] == 4
    assert client.get(GRAPH, params={'version': decision['version_id']}).json()['total_nodes'] == 3
    assert listing(client)['decisions'][0]['status'] == 'revoked'


def test_subgroup_distinct_blocks_transitive_merge_and_revoke_reopens_pair(client):
    publish(client, [candidate_unit(s, [(s, ['code'])]) for s in ('one', 'two', 'three')])
    review = listing(client)
    candidate, = review['candidates']
    a, b, c = candidate['members']
    first = submit(client, review, candidate, verdict='different', members=[a, c])
    assert first.status_code == 200
    review = listing(client)
    assert not review['candidates'][0]['has_active_decision']
    merged = submit(client, review, candidate, members=[a, b])
    assert merged.status_code == 200, merged.text
    assert client.get(GRAPH).json()['total_nodes'] == 2
    review = listing(client)
    assert review['candidates'][0]['has_active_decision']
    assert submit(client, review, candidate, members=[a, b, c]).status_code == 409
    assert revoke(client, review, first.json()).status_code == 200
    review = listing(client)
    assert not review['candidates'][0]['has_active_decision']
    assert submit(client, review, candidate, members=[b, c]).status_code != 200
    assert submit(client, review, candidate, members=[a, b, c]).status_code == 200
    assert client.get(GRAPH).json()['total_nodes'] == 1


@pytest.mark.parametrize('limited', ['scope', 'context'])
def test_hidden_support_cannot_be_decided_or_disclosed(client, limited):
    units = [candidate_unit(s, [(s, ['code'])]) for s in ('a', 'b')]
    node = units[0]['graph']['nodes'][0]
    extra = deepcopy(node['supports'][0])
    extra['properties']['note'] = ['do-not-disclose']
    if limited == 'scope':
        extra['scope_ref'] = 'secret'
    else:
        extra['business_context']['dimensions']['project'] = 'other'
    node['supports'].append(extra)
    publish(client, units)
    params = {'business_context': '{"project":"M"}'} if limited == 'context' else {}
    review = listing(client, **params)
    candidate, = review['candidates']
    assert 'do-not-disclose' not in str(review)
    body = {'business_context': {'project': 'M'}} if limited == 'context' else {}
    result = submit(client, review, candidate, **body)
    assert result.status_code == 409 and result.json()['error']['code'] == 'resolution_scope_conflict'
    assert client.get(GRAPH).json()['version_id'] == review['version_id']


def test_two_original_relationships_survive_when_both_ends_are_manually_identified(client):
    publish(client, [unit(s, projection('Record', 'code', table=s)) for s in ('a', 'b')])
    original = client.get(GRAPH + '/export').json()
    assert len(original['entities']) == 4 and len(original['relationships']) == 2
    first = listing(client)
    assert len(first['candidates']) == 2
    for candidate in first['candidates']:
        response = submit(client, listing(client), candidate)
        assert response.status_code == 200, response.text
    after = client.get(GRAPH + '/export').json()
    assert len(after['entities']) == 2 and len(after['relationships']) == 2
    assert len({(e['source'], e['type'], e['target']) for e in after['relationships']}) == 1
    assert len({e['id'] for e in after['relationships']}) == 2
    lineage = {s['edge_id'] for e in after['relationships'] for witness in e['metadata']['supports']
               for s in witness.get('endpoint_origins', [])}
    assert all(e['id'] in lineage or e['id'] in {new['id'] for new in after['relationships']}
               for e in original['relationships'])


def test_failed_publication_keeps_graph_and_ledger_together_and_new_extraction_resets_review(client, monkeypatch):
    units = [candidate_unit(s, [(s, ['code'])]) for s in ('a', 'b')]
    publish(client, units)
    first = listing(client)
    from lightgraph.web import publication_identity
    from lightgraph.errors import LightGraphError
    def fail(*_args, **_kwargs):
        raise LightGraphError('graph_parquet_write_failed')
    with monkeypatch.context() as patch:
        patch.setattr(publication_identity, 'write_graph_record', fail)
        failed = submit(client, first, first['candidates'][0])
        assert failed.status_code != 200
    assert listing(client) == first
    assert submit(client, first, first['candidates'][0]).status_code == 200
    previous = listing(client)
    publish(client, units)
    current = listing(client)
    assert current['revision'] == 0 and current['decisions'] == []
    assert current['candidates'][0]['candidate_id'] != first['candidates'][0]['candidate_id']
    assert revoke(client, previous, previous['decisions'][0]).status_code == 409
    assert client.get(GRAPH, params={'version': previous['version_id']}).json()['total_nodes'] == 1


def test_revoked_source_project_grant_blocks_review_of_existing_derived_decisions(client):
    from pathlib import Path
    from lightgraph.construction.inheritance import freeze_inherited_graphs
    from lightgraph.tests.test_web_graph_history import publish as publish_source
    workspace = client.app.state.workspace
    workspace.create('source project', 'base')
    publish_source(workspace.result_config('base'), 'grant')
    config = workspace.get_config('p')
    config['inherited_graphs'] = [{'inherit_id': 'base', 'source_project_id': 'base', 'allowed_scopes': ['public']}]
    workspace.save_config('p', config)
    derived = workspace.result_config('p')
    freeze_inherited_graphs(derived, Path(derived.output_dir) / 'construction', workspace=workspace)
    publish(client, [candidate_unit(s, [(s, ['code'])]) for s in ('one', 'two')])
    before = listing(client)
    accepted = submit(client, before, before['candidates'][0])
    assert accepted.status_code == 200, accepted.text
    after = listing(client)
    withdrawn = workspace.get_config('base')
    withdrawn['scopes'] = ['withdrawn']
    workspace.save_config('base', withdrawn)
    for response in (client.get(REVIEW), revoke(client, after, accepted.json()), client.get(GRAPH),
                     client.get(GRAPH + '/export', params={'version': before['version_id']})):
        assert response.json()['error']['code'] == 'inherited_graph_scope_not_authorized'


def test_cross_type_review_specializes_endpoints_and_rejects_a_new_parent_self_link(client):
    units = []
    for name, kind in [('a', 'Account'), ('b', 'Entry')]:
        graph = projection(kind, 'code', table=name)
        for node in graph['nodes']:
            node['label'] = node['supports'][0]['label'] = '共同名称'
        units.append(unit(name, graph))
    publish(client, units)
    listing_before = listing(client)
    group, = listing_before['candidates']
    originals = client.get(GRAPH + '/export').json()
    parents = [e['source'] for e in originals['relationships']]
    edge = originals['relationships'][0]
    conflict = submit(client, listing_before, group, members=[edge['source'], edge['target']])
    assert conflict.status_code == 409 and conflict.json()['error']['code'] == 'resolution_hierarchy_conflict'
    assert listing(client)['version_id'] == listing_before['version_id']
    decision = submit(client, listing_before, group, members=parents)
    assert decision.status_code == 200, decision.text
    cfg = client.app.state.workspace.result_config('p')
    from lightgraph.web.construction_graph import load_graph
    result = load_graph(cfg)
    nodes = {n['id']: n for n in result['nodes']}
    relations = {r['type']: r for r in result['definitions']['relations']}
    assert len(nodes) == 3 and len(result['edges']) == 2
    assert len({e['source'] for e in result['edges']}) == 1
    for edge in result['edges']:
        definition = relations[edge['type']]
        assert definition['source_type'] == nodes[edge['source']]['type']
        assert definition['target_type'] == nodes[edge['target']]['type']


def test_broader_reviewer_cannot_publish_identity_evidence_into_a_narrower_view(client):
    ws = client.app.state.workspace
    config = ws.get_config('p')
    config['scopes'] = ['public', 'private']
    ws.save_config('p', config)
    units = [candidate_unit(s, [(s, ['code'])]) for s in ('one', 'two')]
    for item in units:
        node = item['graph']['nodes'][0]
        confidential = deepcopy(node['supports'][0])
        confidential.update(scope_ref='private', properties={'review_key': ['restricted']})
        node['supports'].append(confidential)
    publish(client, units)
    review = listing(client)
    candidate, = review['candidates']
    assert 'restricted' in str(candidate)
    for verdict in ('same', 'different'):
        result = submit(client, review, candidate, verdict=verdict)
        assert result.status_code == 409 and result.json()['error']['code'] == 'resolution_scope_conflict'
    config['scopes'] = ['public']
    ws.save_config('p', config)
    assert 'restricted' not in str(listing(client))
    assert client.get(GRAPH).json()['total_nodes'] == 2


def test_late_review_conflict_removes_only_the_uncommitted_graph(client, monkeypatch):
    from pathlib import Path
    from lightgraph.errors import LightGraphError
    from lightgraph.web import publication_identity

    publish(client, [candidate_unit(s, [(s, ['code'])]) for s in ('a', 'b')])
    original = listing(client)
    directory = Path(client.app.state.workspace.result_config('p').output_dir) / 'construction' / 'published'
    before = set(directory.iterdir())
    authorize = publication_identity.Review.authorize

    def changed_after_prepare(self, workspace, config):
        if hasattr(self, 'groups'):
            raise LightGraphError('resolution_version_conflict')
        return authorize(self, workspace, config)

    with monkeypatch.context() as patch:
        patch.setattr(publication_identity.Review, 'authorize', changed_after_prepare)
        response = submit(client, original, original['candidates'][0])
        assert response.status_code == 409
    assert listing(client) == original and set(directory.iterdir()) == before
