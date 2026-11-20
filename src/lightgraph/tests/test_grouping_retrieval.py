"""Grouping finds relevant published catalogs without inventing aggregate values."""
import pytest

from lightgraph.retrieval.grouping import grouping_spans

from lightgraph.retrieval import SubgraphRetriever, sync_graph_to_retrieval
from lightgraph.retrieval.query import RetrieveSubgraphRequest


@pytest.mark.parametrize(('query', 'subject'), [('分子的结构', '结构'), ('部分样本的来源', '来源')])
def test_bare_operator_inside_noun_does_not_create_grouping(query, subject):
    assert grouping_spans(query, [{'text': subject}]) is None


def _index(tmp_path, subject, axis, *, include_parser=None, mismatch=False, unknown=False):
    def support(label, project='M', grant='read'):
        if unknown:
            return {'scope_ref': grant, 'label': label, 'provenance': {'source': {'table': 'source_table'}}}
        return {'scope_ref': grant, 'label': label, 'business_context': {
            'status': 'scoped', 'dimensions': {'project': project}}}
    def node(nid, kind, name, **kw):
        return {'id': nid, 'type': kind, 'identity': {'id': nid}, 'supports': [support(name, **kw)]}
    def edge(eid, src, dst, rel, **kw):
        return {'id': eid, 'source': src, 'target': dst, 'type': rel, 'supports': [support('', **kw)]}

    catalog = {'support_visibility_version': 1, 'definitions': {},
        'nodes': [node('subject', '记录', subject), node('base', '定义', subject + '定义'),
                  node('A', '分类', axis + '标准目录'), node('B', '分类', '地方' + axis + '目录'),
                  node('region', '范围', '西区'), node('other', '字段', axis), node('hidden', '分类', axis + '保密目录', grant='hidden')],
        'edges': [edge('definition', 'subject','base','采用定义'),
                  edge('map-A', 'base','A','适用分类', project='K' if mismatch else 'M'),
                  edge('map-B', 'B','base','分类适用于'),
                  edge('private', 'base','hidden','适用分类', grant='hidden')]}
    sync_graph_to_retrieval(catalog, tmp_path)
    return SubgraphRetriever(tmp_path, mention_extractor=include_parser)


@pytest.mark.parametrize(('question','subject','axis'), [
    ('分产品的收入各是多少', '收入', '产品'),
    ('维修次数按设备分组', '维修次数', '设备'),
    ('按年代统计馆藏数', '馆藏数', '年代'),
    ('Sightings grouped by habitat', 'Sightings', 'habitat'),
])
def test_grouping_query_keeps_published_catalogs_separate(tmp_path, question, subject, axis):
    index = _index(tmp_path, subject, axis)
    ans = RetrieveSubgraphRequest(query=question).run(index, ['read'])
    assert ans['grouping']['axis'] == axis and ans['grouping']['value_status'] == 'not_computed'
    assert {p['target_id'] for p in ans['grouping']['paths']} == {'A','B'}
    assert {e['id'] for e in ans['edges']} == {'definition','map-A','map-B'}
    assert all(not p['observed_path'] for p in ans['grouping']['paths'])
    assert 'hidden' not in str(ans) and 'other' not in {n['id'] for n in ans['nodes']}


def test_filters_prevent_paths_from_being_presented_as_joint_results(tmp_path):
    index = _index(tmp_path, '维修次数', '设备', mismatch=True)
    answer = RetrieveSubgraphRequest(query='西区各设备的维修次数').run(index, ['read'])
    assert answer['grouping']['filters'] == ['西区']
    assert answer['nodes'] == answer['edges'] == []
    assert {n['id'] for n in answer['available_context']['nodes']} == {'subject', 'base', 'B', 'region'}
    assert [p['target_id'] for p in answer['grouping']['paths']] == ['B']
    tiny = RetrieveSubgraphRequest(query='分设备的维修次数', max_depth=1).run(index, ['read'])
    assert not tiny['grouping']['paths'] and not tiny['edges']


def test_model_can_supply_an_open_domain_grouping_outside_offline_grammar(tmp_path):
    subject, axis = '馆藏数', '年代'
    parser = lambda _: {'intent': 'grouping', 'mentions': [subject], 'target': axis,
                        'constraints': [], 'framing': ['给', '来个', '分布']}
    index = _index(tmp_path, subject, axis, include_parser=parser)
    answer = RetrieveSubgraphRequest(query='给馆藏数来个年代分布').run(index, ['read'])
    assert answer['plan']['parser'] == 'model' and {p['target_id'] for p in answer['grouping']['paths']} == {'A','B'}

def test_unscoped_table_source_supports_only_published_associations(tmp_path):
    index = _index(tmp_path, '借阅次数', '文类', unknown=True)
    result = RetrieveSubgraphRequest(query='各文类的借阅次数').run(index, ['read'])
    assert result['grouping']['grouping_applicability'] == 'unverified'
    assert {p['context_status'] for p in result['grouping']['paths']} == {'unverified'}
    assert all(e['source_tables'] and not e['evidence_refs'] for e in result['edges'])
    scoped = RetrieveSubgraphRequest(query='各文类的借阅次数', applicability={'project': 'M'}).run(index, ['read'])
    assert not scoped['edges'] and 'grouping' not in scoped


def test_named_predicate_and_traversal_not_overridden(tmp_path):
    index = _index(tmp_path, '馆藏数', '年代')
    for q in ['只沿入边查馆藏数按年代分组', '下钻后各年代的馆藏数',
              '只走引用路径，各年代的馆藏数', '排除南馆，按年代统计馆藏数']:
        answer = RetrieveSubgraphRequest(query=q).run(index, ['read'])
        assert 'grouping' not in answer


def test_embedded_ascii_fragment_is_not_a_grouping_name(tmp_path):
    index = _index(tmp_path, 'Loans', 'particle')
    answer = RetrieveSubgraphRequest(query='Loans grouped by art').run(index, ['read'])
    assert not answer['edges'] and not answer.get('grouping', {}).get('paths')


def test_blind_grouping_alias_can_suggest_but_not_confirm_a_catalog(tmp_path):
    from lightgraph.retrieval.mentions import QueryMentions

    question = '给馆藏数来个编年分布'
    parsed = QueryMentions.model_validate({
        'mentions': ['馆藏数'], 'target': '编年', 'constraints': [], 'framing': ['给', '来个', '分布'],
        'intent': 'grouping', 'overview_metric': None, 'scopes': [], 'background': [], 'exclusions': [],
        'equivalents': [{'text': '编年', 'names': ['年代标准目录']}],
    }).verified(question)
    index = _index(tmp_path, '馆藏数', '年代', include_parser=lambda _: parsed)
    result = RetrieveSubgraphRequest(query=question).run(index, ['read'])
    path, = result['grouping']['paths']
    assert path['target_id'] == 'A' and path['match'] == 'semantic_candidate'
    assert result['grouping']['grouping_applicability'] == 'unverified'


def test_complete_distribution_phrase_does_not_turn_framing_into_filter(tmp_path):
    def parser(question):
        return {'intent': 'grouping', 'mentions': ['馆藏数'], 'target': '年代',
                'constraints': [], 'framing': ['在不同']}

    # A small model may treat a grammatical connector as an additional condition.
    # Only a complete independently parsed question can override that judgment.
    parser.review_coverage = lambda _question, _fragments: set()
    index = _index(tmp_path, '馆藏数', '年代', include_parser=parser)
    q = '给我看看馆藏数在不同年代的分布'
    answer = RetrieveSubgraphRequest(query=q).run(index, ['read'])
    assert answer['grouping']['filters'] == [] and len(answer['edges']) == 3
    filtered = RetrieveSubgraphRequest(query=q + '，排除未编目样本').run(index, ['read'])
    assert not filtered['edges']
