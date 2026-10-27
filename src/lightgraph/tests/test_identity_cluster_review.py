"""Automatic identity review must consider an entire growing occurrence group."""
import pytest

from lightgraph.contracts import ExtractionGoal, GraphFact, PartitionCompletion, SupportSpec
from lightgraph.documents import DocumentCorpus
from lightgraph.errors import LightGraphError
from lightgraph.identity.reconciliation import record_candidates, review_with_agent, list_candidates
from lightgraph.store import GraphStore


class OfflineModel:
    allow_source_samples = True

    def complete(self, *args, **kwargs):
        raise AssertionError('No real model is used in this control-flow regression')


def seed(store):
    publication = store.begin_publication('occurrences', 1, 'all', {})
    support = SupportSpec(origin='user_input', source_binding='occurrences', scope_ref='public',
        partition_id='all', rule_id='occurrences', rule_revision=1)
    for entity in ['a', 'b', 'c']:
        store.stage_fact(publication, GraphFact(fact_kind='NodeExistence', payload={
            'entity_id': entity, 'kind': 'ReportItem', 'layer': 'business'}), support)
        for name, value in [('label', 'Shared object'), ('definition', 'Approved common definition')]:
            store.stage_fact(publication, GraphFact(fact_kind='PropertyValue', payload={
                'entity_id': entity, 'name': name, 'value': value}), support)
    store.publish_partition(PartitionCompletion(publication_id=publication, complete=True,
        consistent=True, dependencies_valid=True))
    record_candidates(store, store.view({'public'}, apply_resolutions=False), {'public'})


@pytest.mark.parametrize('cluster_supported', [True, False])
def test_three_occurrences_review_whole_cluster_and_do_not_repeat(monkeypatch, cluster_supported):
    reviewed = []
    def review(model, packet, proposition, documents):
        members = packet['authorized_entities']
        reviewed.append(list(members))
        assert {fact['payload'].get('entity_id') for fact in packet['graph_facts']} == set(members)
        if len(members) == 3:
            assert proposition['members'] == ['a', 'b', 'c']
            if not cluster_supported:
                raise LightGraphError('semantic_review_unresolved')
        return [fact['reference'] for fact in packet['graph_facts']
                if fact['payload'].get('name') == 'definition']
    monkeypatch.setattr('lightgraph.discovery.semantics.review_semantics', review)
    with GraphStore() as store:
        seed(store)
        counts, issues = review_with_agent(store, OfflineModel(), ExtractionGoal(), DocumentCorpus([]), scopes={'public'})
        assert counts['reviewed'] == 2
        assert counts['accepted'] == (2 if cluster_supported else 1)
        assert len(reviewed[0]) == 2 and reviewed[1] == ['a', 'b', 'c']
        assert not any(i['code'] == 'resolution_cluster_review_required' for i in issues)
        assert len(store.view({'public'})['nodes']) == (1 if cluster_supported else 2)
        again, _ = review_with_agent(store, OfflineModel(), ExtractionGoal(), DocumentCorpus([]), scopes={'public'})
        assert again['reviewed'] == 0 and len(reviewed) == 2
        if cluster_supported:
            assert any(len(d['members']) == 3 and d['status'] == 'active'
                       for d in list_candidates(store, {'public'})['decisions'])


def test_transient_identity_commit_failure_is_retryable(monkeypatch):
    def review(model, packet, proposition, documents):
        return [fact['reference'] for fact in packet['graph_facts']
                if fact['payload'].get('name') == 'definition']
    monkeypatch.setattr('lightgraph.discovery.semantics.review_semantics', review)
    import lightgraph.identity.reconciliation as reconciliation
    original = reconciliation.decide
    def concurrent_change(*args, **kwargs):
        raise LightGraphError('resolution_revision_conflict')
    monkeypatch.setattr(reconciliation, 'decide', concurrent_change)
    with GraphStore() as store:
        seed(store)
        counts, _ = review_with_agent(store, OfflineModel(), ExtractionGoal(), DocumentCorpus([]), scopes={'public'})
        assert counts['deferred'] > 0 and counts['accepted'] == 0
        monkeypatch.setattr(reconciliation, 'decide', original)
        recovered, _ = review_with_agent(store, OfflineModel(), ExtractionGoal(), DocumentCorpus([]), scopes={'public'})
        assert recovered['accepted'] == 2
        assert len(store.view({'public'})['nodes']) == 1
