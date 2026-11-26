"""Public query-only behavior with a controlled semantic space and real graph evidence."""
import numpy as np
import pytest

from lightgraph.retrieval import SubgraphRetriever, sync_graph_to_retrieval
from lightgraph.retrieval.query import RetrieveSubgraphRequest


class MaintenanceEmbedder:
    """A deterministic model double: close paraphrases and an intentionally tied pair."""

    groups = (
        ('装配车间', 'AssemblyBay'), ('空压机', '空气压缩机', 'AirCompressor', 'air compressor', '气源机乙', '专用气源台'),
        ('轴承过热', 'BearingOverheating'), ('止推轴承',), ('排气阀',), ('本次检修',),
    )
    dim = len(groups) + 1

    def embed(self, texts):
        embeddings = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in zip(embeddings, texts):
            for index, group in enumerate(self.groups):
                if any(term.casefold() in text.casefold() for term in group):
                    row[index] = 1
            if not row.any():
                row[-1] = 1
            row /= np.linalg.norm(row)
        return embeddings

    embed_queries = embed


def maintenance_graph(*, ambiguous=False):
    codes = {'job': 'J1', 'place': 'A1', 'machine': 'P1', 'fault': 'H1',
             'bearing': 'B1', 'valve': 'V1'}

    def support(label, project='M', grant='public'):
        return {'scope_ref': grant, 'label': label, 'properties': {},
                'business_context': {'version': 1, 'status': 'scoped', 'dimensions': {'project': project}}}

    def node(key, kind, label, code, project='M', grant='public'):
        return {'id': key, 'type': kind, 'identity': {'code': code},
                'supports': [support(label, project, grant)]}

    nodes = [node(key, kind, label, codes[key]) for key, kind, label in [
        ('job', '记录', '本次检修'), ('place', '车间', '装配车间'), ('machine', '设备', '空压机'),
        ('fault', '故障', '轴承过热'), ('bearing', '零件', '止推轴承'), ('valve', '零件', '排气阀')]]
    nodes.extend([node('off-project', '设备', '专用气源台', 'PK', project='K'),
                  node('restricted', '设备', '专用气源台', 'PS', grant='secret')])
    if ambiguous:
        nodes.append(node('rival', '设备', '气源机乙', 'P2'))

    edges = []
    for source, relation, target, row in [
        ('job', 'located_at', 'place', 'job1'), ('job', 'records', 'fault', 'job1'),
        ('job', 'uses', 'machine', 'job1'), ('machine', 'has_child', 'bearing', 'job1'),
        ('machine', 'has_child', 'valve', 'job2')]:
        obs = support('')
        obs.update(row_context={'id': row, 'values': dict(codes)},
                   provenance={'source': {'source_id': 'plant', 'schema': 'public', 'table': 'work_orders'},
                               'evidence_refs': ['record-' + row], 'context_columns': sorted(codes),
                               'source_key_columns': [source], 'target_key_columns': [target]})
        edges.append({'id': f'{source}-{target}', 'source': source, 'type': relation,
                      'target': target, 'supports': [obs]})
    return {'support_visibility_version': 1, 'definitions': {'entities': [], 'relations': []},
            'nodes': nodes, 'edges': edges}


_QUERY = 'M 项目中，装配车间的空气压缩机轴承过热，对应哪些零件？'


def ask(tmp_path, query, *, semantic=True, ambiguous=False, extractor=None):
    embedder = MaintenanceEmbedder() if semantic else None
    sync_graph_to_retrieval(maintenance_graph(ambiguous=ambiguous), tmp_path, embedder=embedder)
    kwargs = {'mention_extractor': extractor} if extractor is not None else {}
    retriever = SubgraphRetriever(tmp_path, embedder=embedder, **kwargs)
    return RetrieveSubgraphRequest.model_validate({'query': query}).run(retriever, scopes=['public'])


def assert_grounded_repair(result):
    assert result['plan']['intent'] == 'dimensional'
    assert result['plan']['business_context'] == {'project': 'M'}
    assert result['dimensional']['status'] == 'matched'
    assert result['dimensional']['level_node_ids'] == ['bearing']
    assert result['dimensional']['witnesses'][0]['row_id'] == 'job1'
    assert {n['id'] for n in result['nodes']} == {'job', 'place', 'machine', 'bearing', 'fault'}
    assert 'off-project' not in str(result)
    assert 'restricted' not in str(result)


def test_unlisted_synonym_is_a_scoped_condition_in_joint_drilldown(tmp_path):
    def parse(_):
        return {'mentions': ['装配车间', '空气压缩机', '轴承过热'], 'target': '零件', 'intent': 'dimensional',
                'equivalents': [{'text': '空气压缩机', 'names': ['空压机']}]}
    result = ask(tmp_path, _QUERY, extractor=parse)
    assert_grounded_repair(result)
    slot = next(s for s in result['plan']['conditions'] if s['text'] == '空气压缩机')
    assert slot['match'] == 'semantic'
    assert [c['node_id'] for c in slot['candidates']] == ['machine']


def test_multiword_synonym_keeps_its_words_together_without_chat(tmp_path):
    result = ask(tmp_path, _QUERY.replace('空气压缩机', 'air compressor'))
    assert result['available_context']['status'] == 'partial' and not result['edges']
    assert any(s['text'].endswith('air compressor') and s['candidates'][0]['node_id'] == 'machine'
               for s in result['plan']['suggestions'])


def test_tied_authorized_equipment_is_ambiguous_before_graph_expansion(tmp_path):
    result = ask(tmp_path, _QUERY, ambiguous=True)
    assert result['plan']['intent'] == 'clarify' and result['plan']['unresolved']
    assert not result['nodes'] and not result['edges']
    assert 'rival' in str(result['plan']) and 'machine' in str(result['plan'])
    assert 'restricted' not in str(result) and 'off-project' not in str(result)


def test_local_hash_cannot_confirm_synonym_as_a_joint_fact(tmp_path):
    result = ask(tmp_path, _QUERY, semantic=False)
    assert result['plan']['intent'] == 'clarify' and result['plan']['unresolved']
    assert not result['nodes'] and not result['edges']


def test_model_recovers_contiguous_mentions_without_any_graph_spelling(tmp_path):
    mentions = ['AssemblyBay', 'AirCompressor', 'BearingOverheating']
    query = 'M 项目中，' + ''.join(mentions) + '，对应哪些零件？'

    def phrases(text):
        assert all(term in text for term in mentions)
        return {'mentions': mentions, 'target': '零件', 'constraints': [], 'intent': 'dimensional', 'framing': [],
                'equivalents': [{'text': m, 'names': [n]} for m, n in zip(mentions, ['装配车间', '空压机', '轴承过热'])]}

    result = ask(tmp_path, query, extractor=phrases)
    assert_grounded_repair(result)
    assert {s['text'] for s in result['plan']['conditions'] if s.get('match') == 'semantic'} == set(mentions)


def test_whole_literal_entity_survives_model_split_and_project_filter(tmp_path):
    query = 'M 项目里装配车间的空气压缩机轴承过热，对应哪些零件？'

    def phrases(text):
        assert 'M 项目里装配车间' in text
        return {'mentions': ['M 项目', '装配车间', '空气压缩机', '轴承', '过热'],
                'constraints': ['M 项目'], 'target': '零件', 'intent': 'dimensional', 'framing': [],
                'equivalents': [{'text': '空气压缩机', 'names': ['空压机']}]}

    result = ask(tmp_path, query, extractor=phrases)
    assert_grounded_repair(result)
    assert [s['text'] for s in result['plan']['conditions']] == ['装配车间', '空气压缩机', '轴承过热']


def test_coarse_model_phrase_does_not_collapse_two_known_conditions(tmp_path):
    def phrases(_):
        return {'mentions': ['装配车间的空气压缩机轴承过热'], 'constraints': [],
                'target': '零件', 'intent': 'dimensional', 'framing': []}

    result = ask(tmp_path, _QUERY, extractor=phrases)
    assert not result['nodes'] and result['available_context']['status'] == 'partial'
    assert {s['text'] for s in result['plan']['conditions']} == {'装配车间', '轴承过热'}
    assert '空气压缩机' in str(result['plan']['unbound_mentions'])


def test_repeated_categorical_filter_is_proved_by_the_same_joint_evidence(tmp_path):
    def phrases(_):
        return {'mentions': ['装配车间', '空气压缩机', '轴承过热'], 'constraints': ['空气压缩机轴承过热'],
                'target': '零件', 'intent': 'drilldown', 'framing': ['对应哪些'],
                'equivalents': [{'text': '空气压缩机', 'names': ['空压机']}]}

    assert_grounded_repair(ask(tmp_path, _QUERY, extractor=phrases))


@pytest.mark.parametrize(('constraint', 'reported_as_framing'), [
    ('检修时长超过8小时', False), ('检修时长超过8小时', True), ('已通过防爆认证', True),
])
def test_unresolved_predicate_never_disappears_during_linking(tmp_path, constraint, reported_as_framing):
    query = _QUERY.replace('？', '，且' + constraint + '。')

    def phrases(_):
        return {'mentions': ['装配车间', '空气压缩机', '轴承过热'], 'target': '零件', 'intent': 'dimensional',
                'constraints': [] if reported_as_framing else [constraint],
                'framing': [constraint] if reported_as_framing else []}

    result = ask(tmp_path, query, extractor=phrases)
    assert result['plan']['intent'] == 'clarify' and result['plan']['unresolved']
    assert not result['nodes'] and not result['edges']


def test_absent_concept_never_becomes_a_fact_or_a_new_node(tmp_path):
    def parse(_):
        return {'mentions': ['solstice quark fennec'], 'intent': 'search'}
    result = ask(tmp_path, 'solstice quark fennec', extractor=parse)
    assert not result['nodes'] and not result['edges']
    assert result['available_context']['status'] == 'no_confirmed_match'
    assert result['plan']['unbound_mentions'][0]['text'] == 'solstice quark fennec'


def test_omitted_constraint_in_same_clause_is_reported_without_losing_known_entity(tmp_path):
    def parse(_):
        return {'mentions': ['装配车间'], 'intent': 'search'}
    parse.review_coverage = lambda query, fragments: []
    result = ask(tmp_path, 'M 项目中，装配车间须满足蓝标规范', extractor=parse)
    assert not result['nodes'] and not result['edges']
    assert '须满足蓝标规范' in str(result['plan']['unbound_mentions'])
    node, = result['available_context']['nodes']
    assert (node['id'], node['display_name']) == ('place', '装配车间')
    obs, = node['observations']
    assert obs['label'] == '装配车间' and obs['business_context']['dimensions'] == {'project': 'M'}


def test_unregistered_framing_is_not_treated_as_a_missing_entity(tmp_path):
    def parse(_):
        return {'mentions': ['装配车间', '空压机', '轴承过热'], 'target': '零件', 'intent': 'dimensional'}
    def coverage(query, fragments):
        assert '那台' in fragments and '牵涉到哪些' in fragments
        return fragments
    parse.review_coverage = coverage
    result = ask(tmp_path, 'M 项目中，装配车间那台空压机，轴承过热牵涉到哪些零件？', extractor=parse)
    assert_grounded_repair(result)
    assert not result['plan']['unbound_mentions']


def test_unresolved_model_scope_returns_no_nearest_nodes_or_context(tmp_path):
    def parse(_):
        return {'mentions': ['装配车间'], 'intent': 'search',
                'scopes': [{'text':'无此研究组', 'dimension':'研究组', 'value':'无此'}]}
    result = ask(tmp_path, '无此研究组的装配车间', extractor=parse)
    assert result['available_context']['status'] == 'scope_unresolved'
    assert not result['available_context']['nodes'] and not result['plan']['suggestions']


def test_model_puts_anchor_in_filter_and_type_in_output_wording(tmp_path):
    def parse(_):
        return {'mentions': ['空气压缩机轴承过热'], 'constraints': ['M 项目中', '装配车间'],
                'target': None, 'intent': 'drilldown', 'framing': ['对应哪些零件？']}
    def review(_, spans):
        assert '对应哪些' not in spans and '的' not in spans
        return spans
    parse.review_coverage = review
    result = ask(tmp_path, _QUERY, extractor=parse)
    plan = result['plan']
    assert plan['requested_level'] == '零件'
    assert not plan['unverified_predicates']
    assert {s['text'] for s in plan['conditions']} == {'轴承过热', '装配车间'}
    assert [m['text'] for m in plan['unbound_mentions']] == ['空气压缩机']
    assert result['available_context']['candidate_path']['level_node_ids'] == ['bearing']
    assert not result['nodes']


def test_filter_inside_model_output_phrase_survives_type_binding(tmp_path):
    def parse(_):
        return {'mentions': ['空压机'], 'constraints': [], 'target': '经过盐雾试验的零件', 'intent': 'drilldown'}
    result = ask(tmp_path, '空压机经过盐雾试验的零件', extractor=parse)
    assert result['plan']['requested_level'] == '零件'
    assert '经过盐雾试验的' in result['plan']['unverified_predicates']
    assert not result['edges']
