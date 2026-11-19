import pytest

from lightgraph.retrieval.intent import _clauses, _mask_scopes, _residuals, _scope_matches
from lightgraph.retrieval.retriever import decompose_query
from lightgraph.retrieval.store_sqlite import normalize_term


@pytest.mark.parametrize('phrase', ['查理', '和田玉', '查看器', '请问科技', '内容管理系统', '空气中微粒',
                                    '为什么科技', '贸易及服务', 'AC/DC', 'air compressor'])
def test_unregistered_phrase_survives_both_offline_paths(phrase):
    normalized = normalize_term(phrase)
    assert _residuals(phrase, [], None) == [normalized]
    assert [s.normalized for s in decompose_query(phrase)] == [normalized]
    # Proven punctuation and an independent entity create an unambiguous gap.
    text = '样品甲，' + phrase
    assert _residuals(text, [{'text': '样品甲', 'role':'样品'}], None) == [normalized]
    assert [s.normalized for s in decompose_query(text, known_terms=['样品甲'])] == ['样品甲', normalized]


def test_operators_and_type_names_only_consume_whole_gaps():
    slots = [{'text':'示例厂房', 'role':'车间'}]
    assert _residuals('查询示例厂房，空气中微粒车间管理系统', slots, None) == ['空气中微粒车间管理系统']
    assert _residuals('查询示例厂房的空气压缩机，按车间下钻到零件', slots, '零件') == ['空气压缩机']
    assert _clauses('背景音乐排除法') == (['背景音乐排除法'], [])


def test_project_literal_only_masked_at_recognized_occurrence():
    text = '只看 M 项目，参考 AM 项目'
    matches = list(_scope_matches(text, [('project', 'M')]))
    assert len(matches) == 1
    assert 'AM 项目' in _mask_scopes(text, matches)


def test_short_scope_values_do_not_select_an_unregistered_chinese_name():
    assert not list(_scope_matches('南京项目的材料', [('project', '京')]))
    assert not list(_scope_matches('项目=华中', [('project', '华')]))
    assert list(_scope_matches('在京项目中查询材料', [('project', '京')]))
