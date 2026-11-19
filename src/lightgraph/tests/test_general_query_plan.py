"""Scope, hierarchical queries, co-observations, and association paths are independent."""
import pytest

from lightgraph.retrieval import SubgraphRetriever, sync_graph_to_retrieval
from lightgraph.retrieval.intent import build_plan
from lightgraph.retrieval.mentions import QueryMentions


@pytest.fixture
def catalogue(tmp_path):
    def record(label):
        return {'label': label, 'scope_ref': 'public', 'properties': {},
                'business_context': {'status': 'scoped', 'dimensions': {'版本': '初刊'}}}

    types = [('a', '书目', '山行札记'), ('b', '序言', '观星序'), ('c', '书目', '天象录'),
             ('d', '校本', '校勘记'), ('p', '条目', '阿波罗项目'), ('f', '条目', '初刊版本')]
    graph = {'support_visibility_version': 1, 'definitions': {'entities': [], 'relations': []},
             'nodes': [{'id': nid, 'identity': {'key': nid}, 'type': kind, 'supports': [record(label)]}
                       for nid, kind, label in types],
             'edges': [{'id': x+y, 'type': '参照', 'source': x, 'target': y, 'supports': [record('')]}
                       for x, y in [('a','b'), ('b','c'), ('b','d')]]}
    sync_graph_to_retrieval(graph, tmp_path)
    def run(question, interpretation=None, **kwargs):
        def parse(_):
            return interpretation
        # A small parser can misclassify even simple complete request wording.
        parse.review_coverage = lambda query, spans: []
        retriever = SubgraphRetriever(tmp_path, mention_extractor=parse if interpretation else None)
        return build_plan(retriever, ['public'], question, None, **kwargs)
    return run


@pytest.mark.parametrize(('query', 'mentions', 'constraints', 'framing', 'path', 'expected'), [
    ('从山行札记到天象录有哪些关联路径？', ['山行札记','天象录'], [], ['有哪些关联路径？'], None, 'c'),
    ('只看初刊版本，山行札记怎样经由观星序联系到天象录？', ['山行札记','观星序','天象录'],
     ['只看初刊版本'], ['怎样经由'], {'start':'山行札记','end':'天象录','via':['观星序'],
                             'relation':'联系','direction':'both','direction_text':'经由'}, 'c'),
    ('从山行札记到校勘记的路径是什么？', ['山行札记','校勘记'], [], ['到'],
     {'start':'山行札记','end':'校勘记','via':[]}, 'd'),
])
def test_manuscript_traversal_with_incomplete_model_annotations(catalogue, query, mentions, constraints,
                                                               framing, path, expected):
    plan = catalogue(query, {'mentions': mentions, 'constraints': constraints, 'framing': framing,
                             'intent': 'traverse', 'traversal': path})
    assert plan['intent'] == plan['operation'] == 'traverse', plan['unresolved']
    assert (plan['start_id'], plan['target_id']) == ('a', expected)
    if constraints:
        assert plan['business_context'] == {'版本': '初刊'} and plan['via_ids'] == ['b']
    else:
        assert plan['business_context'] is None


def test_ordinary_list_of_different_object_types_does_not_require_one_observation(catalogue):
    query = '山行札记、观星序'
    assert catalogue(query)['intent'] == 'search'
    assert catalogue(query, {'mentions':['山行札记','观星序'], 'intent':'search'})['intent'] == 'search'
    assert catalogue(query, {'mentions':['山行札记','观星序'], 'intent':'cooccurrence'})['intent'] == 'dimensional'


def test_project_can_be_part_of_a_name_in_a_nonproject_graph(catalogue):
    for parsed in (None, {'mentions':['阿波罗项目','山行札记'], 'intent':'traverse'}):
        plan = catalogue('从阿波罗项目到山行札记的路径', parsed)
        assert plan['intent'] == 'traverse' and plan['business_context'] is None
        assert plan['start_id'] == 'p'


def test_applicability_phrase_can_be_the_exact_object_or_a_scoped_path_endpoint(catalogue):
    plan = catalogue('初刊版本')
    assert plan['business_context'] is None
    assert plan['conditions'][0]['candidates'][0]['node_id'] == 'f'
    # Even when the parser mislabels the endpoint as a scope and omits the
    # entity from mentions, the explicit path operand must not disappear.
    parsed = {'intent':'traverse', 'mentions':['天象录'], 'scopes':[
        {'text':'初刊版本', 'dimension':'版本', 'value':'初刊'}]}
    for interpretation in (None, parsed):
        for qualifier in ('', '只看初刊版本，'):
            query = qualifier + '从初刊版本到天象录的路径'
            plan = catalogue(query, interpretation)
            assert plan['intent'] == 'traverse', plan['unresolved']
            assert plan['business_context'] == ({'版本': '初刊'} if qualifier else None)
            assert (plan['start_id'], plan['target_id']) == ('f', 'c')


def test_custom_dimension_values_are_checked_before_traversal(catalogue):
    plan = catalogue('只看失传版本，山行札记到天象录的路径')
    assert plan['intent'] == 'clarify' and plan['scope_unresolved']


def test_path_preserves_unknown_conditions_and_original_direction(catalogue):
    parsed = {'mentions':['山行札记','天象录'], 'intent':'traverse',
              'traversal': {'start':'山行札记', 'end':'天象录', 'direction':'incoming', 'direction_text':'只沿入边'}}
    query = '只沿入边，从山行札记到天象录的路径'
    plan = catalogue(query, parsed)
    assert plan['intent'] == 'traverse' and plan['direction'] == 'incoming'
    contradictory = {**parsed, 'traversal': {**parsed['traversal'], 'direction':'outgoing'}}
    assert catalogue(query, contradictory)['intent'] == 'clarify'
    assert catalogue(query + '，排除已勘误的', parsed)['intent'] == 'clarify'
    assert catalogue(query + '，刊印在1950年之前', parsed)['intent'] == 'clarify'


def test_only_grounded_relation_names_can_constrain_a_generic_path(catalogue):
    parsed = {'mentions':['山行札记','天象录'], 'intent':'traverse',
              'traversal':{'start':'山行札记','end':'天象录','relation':'参照'}}
    query = '从山行札记到天象录，只沿参照关系查找路径'
    assert catalogue(query, parsed)['relation_types'] == ['参照']
    parsed['traversal']['relation'] = '不存在'
    assert catalogue(query.replace('参照','不存在'), parsed)['intent'] == 'clarify'
    parsed['traversal']['relation'] = '联系'
    assert catalogue(query.replace('参照','联系'), parsed)['intent'] == 'clarify'
    parsed['traversal']['relation'] = '参照'
    assert catalogue('山行札记参照天象录', parsed)['intent'] == 'clarify'


def test_path_parser_requires_roles_and_directions_from_active_question():
    data = dict(mentions=['青册','红册'], intent='traverse', target=None, constraints=[], framing=[],
                overview_metric=None, scopes=[], background=['早先查白册'], exclusions=[], equivalents=[])
    question = '早先查白册。只沿出边，从青册到红册。'
    path = dict(start='青册', end='红册', via=[], relation=None, direction='outgoing', direction_text='只沿出边')
    verify = lambda candidate: QueryMentions(**data, traversal=candidate).verified(question)
    assert verify(path)['traversal']['direction'] == 'outgoing'
    assert verify({**path, 'end':'白册'}) is None
    assert verify({**path, 'direction_text':'入边'}) is None
    assert verify({**path, 'relation':'没有出现的关系'}) is None
