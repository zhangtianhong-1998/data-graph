"""Scope decisions must be grounded in active, verbatim occurrences."""
import pytest

from lightgraph.retrieval import SubgraphRetriever, sync_graph_to_retrieval
from lightgraph.retrieval.intent import _model_scope, _residuals
from lightgraph.retrieval.query import RetrieveSubgraphRequest
from lightgraph.retrieval.retriever import decompose_query


class VisibleProjects:
    cohort_contexts = {value: {'dimensions': {'project': value}} for value in ('京', 'M', 'K')}

    def authorized_cohorts(self, scopes, selection):
        return self.cohort_contexts.keys()


def resolve(query, phrase, value, *, dimension='项目', **annotations):
    parsed = {'scopes': [{'text': phrase, 'dimension': dimension, 'value': value}], **annotations}
    return _model_scope(VisibleProjects(), ['public'], None, query, parsed)


@pytest.mark.parametrize(('query', 'phrase', 'value'), [
    ('南京项目的样品', '京项目', '京'),
    ('南京项目的样品', '南京项目', '京'),
    ('项目=京西，查样品', '项目=京', '京'),
    ('AM 项目的样品', 'M 项目', 'M'),
])
def test_scope_value_inside_another_name_is_unresolved(query, phrase, value):
    assert resolve(query, phrase, value)[2]


def test_repeat_current_scope_after_history_is_unambiguous():
    scope, focus, issues = resolve('先前讨论了京项目。京项目，京项目的样品', '京项目', '京',
                                   background=['先前讨论了京项目'])
    assert scope == {'project': '京'} and not issues
    assert '样品' in focus and '京' not in focus


def test_scope_from_background_cannot_choose_a_project():
    assert resolve('先前讨论了京项目。样品', '京项目', '京', background=['先前讨论了京项目'])[2]
    assert resolve('标记项目。样品。标记项目。', '标记项目', '京', background=['标记项目'])[2]


def test_partial_model_background_cannot_remove_unresolved_scope():
    assert resolve('南京项目的样品；M 项目', 'M 项目', 'M', background=['京项目'])[2]


def test_scope_quote_does_not_erase_extra_conditions():
    scope, focus, issues = resolve('M 项目，设备温度高于200°C', 'M 项目，设备温度高于200°C', 'M')
    assert scope == {'project': 'M'} and not issues
    assert '设备温度高于200°C' in focus


def test_free_form_scope_preserves_the_intervening_qualifier():
    scope, focus, issues = resolve('project restricted to approved contracts in M; pumps',
                                   'project restricted to approved contracts in M', 'M', dimension='project')
    assert scope == {'project': 'M'} and not issues
    assert 'restricted to approved contracts in' in focus


def test_query_entry_blocks_a_model_that_shortens_the_project_name(tmp_path):
    observation = {'scope_ref': 'public', 'label': '样品',
                   'business_context': {'version': 1, 'status': 'scoped', 'dimensions': {'project': '京'}}}
    graph = {'support_visibility_version': 1, 'definitions': {'entities': [], 'relations': []},
             'nodes': [{'id': 'sample', 'type': '样本', 'identity': {'code': 'A'}, 'supports': [observation]}],
             'edges': []}
    sync_graph_to_retrieval(graph, tmp_path)

    def misread(_):
        return {'mentions': ['样品'], 'intent': 'search', 'scopes': [
            {'text': '京项目', 'dimension': '项目', 'value': '京'}]}

    retriever = SubgraphRetriever(tmp_path, mention_extractor=misread)
    result = RetrieveSubgraphRequest(query='南京项目的样品').run(retriever, ['public'])
    assert result['available_context']['status'] == 'scope_unresolved'
    assert not result['available_context']['nodes'] and not result['plan']['suggestions']
    assert not result['nodes'] and not result['edges']


@pytest.mark.parametrize('text', ['cartwheel', 'XP10', 'new_art', 'P10', 'artwork'])
def test_ascii_subwords_are_not_dictionary_mentions(text):
    slots = decompose_query(text, known_terms=['art', 'P1'])
    assert all(slot.source != 'exact_term' for slot in slots)
    assert [slot.surface for slot in slots] == [text.lower()]


def test_ascii_codes_can_touch_chinese_or_punctuation():
    slots = decompose_query('查P1以及(art)', known_terms=['art', 'P1'])
    assert {slot.normalized for slot in slots if slot.source == 'exact_term'} == {'art', 'p1'}


def test_masking_a_known_literal_keeps_unregistered_words_containing_it():
    assert _residuals('art，cartwheel', [{'text': 'art'}], None) == ['cartwheel']
