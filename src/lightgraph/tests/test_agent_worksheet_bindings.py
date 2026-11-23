import json

import pytest

from lightgraph.retrieval.agent_context import agent_context


def _observation(number, rows):
    return {'observation_id': f'obs-{number}', 'source_row_witness': {
                'kind': 'excel_rows_v1', 'snapshot': 'a' * 64, 'rows': rows, 'private_note': 'RAW_CANARY'},
            'row_context': {'values': {'name': 'RAW_CANARY'}},
            'business_context': {'status': 'scoped', 'dimensions': {'area': 'north'}},
            'provenance': {'source': {'source_id': 'book', 'schema': 'public', 'table': 'Sheet1'},
                           'source_key_columns': ['child'], 'target_key_columns': ['parent']}}


def _context(*observations):
    nodes = [{'id': label} for label in ('c', 'p')]
    edges = [{'id': 'e', 'source': 'c', 'target': 'p', 'relation': 'belongs_to',
              'observations': list(observations)}]
    return agent_context({'nodes': nodes, 'edges': edges, 'scopes': ['public']})


def test_agent_bindings_keep_each_visible_observation_and_its_own_complete_rows():
    first = _observation(1, [[3, 6], [9, 9]])
    second = _observation(2, [[16, 17]])
    context = _context(first, second, first)
    result = context['bindings']
    assert [(b['observation_id'], b['source_row_witness']['rows']) for b in result] == [
        ('obs-1', [[3, 6], [9, 9]]), ('obs-2', [[16, 17]])]
    assert all(b['source'] == first['provenance']['source'] for b in result)
    assert all(b['business_context'] == first['business_context'] for b in result)
    assert 'RAW_CANARY' not in json.dumps(result)
    assert not context['truncated'] and context['joint_witnesses'] == []


@pytest.mark.parametrize('projection', ['source_binding', 'reference_only'])
def test_projected_observation_keeps_exact_reference_without_borrowing_rows(projection):
    first = _observation(1, [[9, 9]])
    second = _observation(2, [[10, 10]])
    del second['source_row_witness']
    second['projection'] = projection
    if projection == 'reference_only':
        del second['provenance']
    by_id = {b['observation_id']: b for b in _context(first, second)['bindings']}
    assert by_id['obs-1']['source_row_witness']['rows'] == [[9, 9]]
    assert 'source_row_witness' not in by_id['obs-2']


def test_excessive_rows_keep_id_and_mapping_without_truncating_the_locator():
    first = _observation(1, [[r, r] for r in range(1, 4001, 2)])
    second = _observation(2, [[10, 10]])
    second['provenance']['source']['table'] = 'AnotherSheet'
    context = _context(first, second)
    large, small = context['bindings']
    assert context['truncated'] and 'source_row_witness' not in large
    assert large['ref'] == 'e' and large['observation_id'] == 'obs-1'
    assert large['mapping'] == small['mapping'] and large['business_context'] == small['business_context']
    assert small['source']['table'] == 'AnotherSheet' and small['source_row_witness']['rows'] == [[10, 10]]
    assert sum(len(json.dumps(b, ensure_ascii=False, separators=(',', ':')).encode()) + 1
               for b in context['bindings']) <= 10000


@pytest.mark.parametrize('has_id', [True, False])
def test_repeated_citation_exactly_fitting_the_budget_is_complete(monkeypatch, has_id):
    observation = _observation(1, [[11, 17], [77, 81]])
    if not has_id:
        observation.pop('observation_id')
    expected = _context(observation)['bindings']
    size = len(json.dumps(expected[0], ensure_ascii=False, separators=(',', ':')).encode()) + 1
    monkeypatch.setattr('lightgraph.retrieval.agent_context._BINDING_BYTES', size)
    result = _context(observation, observation)
    assert result['bindings'] == expected and not result['truncated']


@pytest.mark.parametrize('missing_field', ['schema', 'source_id', 'table'])
def test_no_worksheet_citation_without_its_own_complete_source(missing_field):
    observation = _observation(1, [[9, 9]])
    del observation['provenance']['source'][missing_field]
    assert all('source_row_witness' not in b for b in _context(observation)['bindings'])
