"""Exercise mention linking with controlled similarities and a real, scope-filtered index."""
import numpy as np
import pytest

from lightgraph.errors import LightGraphError
from lightgraph.retrieval import SubgraphRetriever, sync_graph_to_retrieval
from lightgraph.retrieval.semantic_grounding import link_mentions


class Embeddings:
    dim = 3

    def __init__(self):
        self.batches = []

    def embed(self, texts):
        self.batches.append(list(texts))
        known = {'设备甲': [1, 0, 0], '同义说法': [1, 0, 0],
                 '制备压缩气体': [0, 1, 0], '产气设备': [0, 1, 0],
                 '近义候选': [.99, .01, 0], '受限名称': [1, 0, 0],
                 '弱关联': [.80, .6, 0]}
        return np.array([known.get(text, [0, 0, 1]) for text in texts], dtype=np.float32)

    def embed_queries(self, texts):
        raise AssertionError('Document-relevance instructions would change entity similarity.')


def support(label, description=None, project='M', scope='public'):
    return {'scope_ref': scope, 'label': label, 'description': description, 'properties': {},
            'business_context': {'status': 'scoped', 'dimensions': {'project': project}}}


def node(nid, *supports):
    return {'id': nid, 'type': '设备', 'identity': {'code': nid}, 'supports': list(supports)}


def reader_at(path, nodes, embedder=None):
    graph = {'support_visibility_version': 1, 'nodes': nodes, 'edges': [], 'definitions': {}}
    sync_graph_to_retrieval(graph, path, embedder=embedder)
    return SubgraphRetriever(path, embedder=embedder)


def test_requires_an_independent_paraphrase_and_a_unique_visible_graph_name(tmp_path):
    encoder = Embeddings()
    reader = reader_at(tmp_path, [
        node('one', support('设备甲', '功能甲'), support('设备甲', scope='team')),
        node('two', support('设备乙', '制备压缩气体')),
        node('foreign', support('受限名称', project='K')),
        node('private', support('近义候选', scope='restricted')),
    ], encoder)
    cohorts = reader.authorized_cohorts(['public', 'team'], {'project': 'M'})
    encoder.batches.clear()
    result = link_mentions(reader, ['同义说法', '产气设备', '弱关联'], cohorts,
                           paraphrases={'同义说法': ['设备甲'], '产气设备': ['设备乙']})
    assert encoder.batches == [['同义说法', '产气设备', '弱关联']]
    assert [item['status'] for item in result] == ['linked', 'linked', 'related']
    assert result[0]['candidates'] == [
        {'node_id': 'one', 'kind': '设备', 'score': 1., 'display_name': '设备甲', 'matched_field': 'name',
         'evidence': 'paraphrase_and_vector_agree', 'peer_score': 0., 'matched_terms': ['设备甲'], 'corroborating_fields': ['name']}]
    assert result[1]['candidates'][0]['node_id'] == 'two'
    assert result[1]['candidates'][0]['matched_field'] == 'description'
    for contexts in [reader.authorized_cohorts(['public'], None),
                     reader.authorized_cohorts(['public', 'restricted'], {'project': 'M'})]:
        [ambiguous] = link_mentions(reader, ['同义说法'], contexts,
                                    paraphrases={'同义说法': ['设备甲', '近义候选', '受限名称']})
        assert ambiguous['status'] == 'ambiguous'
        assert len(ambiguous['candidates']) == 2


def test_hash_and_service_outage_do_not_claim_semantic_evidence(tmp_path, monkeypatch):
    nodes = [node('one', support('设备甲'))]
    for directory, encoder in [('hash', None), ('model', Embeddings())]:
        reader = reader_at(tmp_path / directory, nodes, encoder)
        cohorts = reader.authorized_cohorts(['public'], None)
        if encoder:
            def failed(_):
                raise LightGraphError('embedding_service_unavailable')
            monkeypatch.setattr(encoder, 'embed', failed)
        assert link_mentions(reader, ['设备甲'], cohorts) == [
            {'status': 'unavailable', 'text': '设备甲', 'candidates': []}]


def test_invalid_vector_returns_unavailable_but_wrong_dimension_requires_new_index(tmp_path, monkeypatch):
    encoder = Embeddings()
    reader = reader_at(tmp_path, [node('one', support('设备甲'))], encoder)
    cohorts = reader.authorized_cohorts(['public'], None)
    for broken in [[np.nan, 0., 1.], [0., 0., 0.]]:
        monkeypatch.setattr(encoder, 'embed', lambda texts: np.array([broken]))
        assert link_mentions(reader, ['设备甲'], cohorts)[0]['status'] == 'unavailable'
    monkeypatch.setattr(encoder, 'embed', lambda texts: np.ones((1, 4)))
    with pytest.raises(LightGraphError, match='retrieval_index_stale'):
        link_mentions(reader, ['设备甲'], cohorts)


def test_a_single_vector_cannot_distinguish_absence_from_even_a_perfect_match(tmp_path):
    reader = reader_at(tmp_path, [node('one', support('设备甲'))], Embeddings())
    cohorts = reader.authorized_cohorts(['public'], None)
    [result] = link_mentions(reader, ['同义说法'], cohorts)
    assert result['status'] == 'unmatched' and not result['candidates']
    [with_rewrite] = link_mentions(reader, ['同义说法'], cohorts, paraphrases={'同义说法': ['设备甲']})
    assert with_rewrite['status'] == 'related'
    assert with_rewrite['candidates'][0]['evidence'] == 'proposed_name'


def test_corroboration_uses_rank_not_a_universal_cosine_cutoff(tmp_path):
    reader = reader_at(tmp_path, [node('one', support('设备甲')), node('two', support('设备乙'))], Embeddings())
    cohorts = reader.authorized_cohorts(['public'], None)
    [result] = link_mentions(reader, ['弱关联'], cohorts, paraphrases={'弱关联': ['设备甲']})
    assert result['status'] == 'linked' and result['candidates'][0]['score'] == .8
    assert result['candidates'][0]['matched_terms'] == ['设备甲']


def test_collision_or_vector_disagreement_blocks_automatic_link(tmp_path):
    reader = reader_at(tmp_path, [node('one', support('设备甲')), node('two', support('设备甲')),
                                  node('three', support('设备乙'))], Embeddings())
    cohorts = reader.authorized_cohorts(['public'], None)
    [collision] = link_mentions(reader, ['同义说法'], cohorts, paraphrases={'同义说法': ['设备甲']})
    assert collision['status'] == 'ambiguous'
    assert {c['node_id'] for c in collision['candidates'] if c.get('matched_terms')} == {'one', 'two'}
    [disagreement] = link_mentions(reader, ['同义说法'], cohorts, paraphrases={'同义说法': ['设备乙']})
    assert disagreement['status'] == 'related'
    assert all(c['evidence'] != 'paraphrase_and_vector_agree' for c in disagreement['candidates'])


def test_unverified_paraphrase_uses_only_visible_names_and_can_survive_outage(tmp_path):
    reader = reader_at(tmp_path, [node('one', support('设备甲')), node('two', support('保密名', project='K'))])
    cohorts = reader.authorized_cohorts(['public'], {'project': 'M'})
    [result] = link_mentions(reader, ['问题用词'], cohorts, paraphrases={'问题用词': ['设备甲', '保密名']})
    assert result['status'] == 'unavailable'
    assert result['candidates'] == [{'node_id': 'one', 'kind': '设备', 'score': None, 'matched_field': 'name',
                                      'matched_terms': ['设备甲'], 'display_name': '设备甲', 'evidence': 'proposed_name'}]


def test_competing_generated_names_do_not_disappear_when_one_is_absent(tmp_path):
    reader = reader_at(tmp_path, [node('one', support('设备甲')), node('two', support('设备乙'))], Embeddings())
    cohorts = reader.authorized_cohorts(['public'], None)
    [result] = link_mentions(reader, ['同义说法'], cohorts, paraphrases={'同义说法': ['设备甲', '图上没有的其他释义']})
    assert result['status'] == 'related'
    assert not any(c['evidence'] == 'paraphrase_and_vector_agree' for c in result['candidates'])


def test_distant_query_fails_graph_local_peer_comparison(tmp_path):
    encoder = Embeddings()
    reader = reader_at(tmp_path, [node('one', support('设备甲')), node('near', support('近义候选'))], encoder)
    cohorts = reader.authorized_cohorts(['public'], None)
    [result] = link_mentions(reader, ['弱关联'], cohorts)
    # Both cosine scores exceed .8 or nearly do. Neither candidate is as
    # close to the query as the authorized candidates are to each other.
    assert result == {'text': '弱关联', 'status': 'unmatched', 'candidates': []}


def test_private_peer_does_not_change_public_name_corroboration(tmp_path):
    reader = reader_at(tmp_path, [
        node('one', support('设备甲')), node('two', support('设备乙')),
        node('private', support('近义候选', scope='team'))], Embeddings())
    [public] = link_mentions(reader, ['弱关联'], reader.authorized_cohorts(['public'], None),
                             paraphrases={'弱关联': ['设备甲']})
    assert public['status'] == 'linked'
    assert public['candidates'][0]['peer_score'] == 0 and 'private' not in str(public)
    [with_team] = link_mentions(reader, ['弱关联'], reader.authorized_cohorts(['public', 'team'], None),
                                paraphrases={'弱关联': ['设备甲']})
    assert with_team['status'] == 'related'
