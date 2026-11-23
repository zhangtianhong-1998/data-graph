"""The selected edge's worksheet rows survive a bounded path response when they fit."""
import pytest

from lightgraph.contracts import canonical
from lightgraph.retrieval import SubgraphRetriever, sync_graph_to_retrieval
from lightgraph.retrieval.path_evidence import evidence_preview
from lightgraph.retrieval.query import RetrieveSubgraphRequest
from lightgraph.tests.test_business_context_retrieval import _context, _support


def _observation(name, rows, *, scope='public', rewritten=False):
    observation = _support('', _context('global'), scope_ref=scope, observation_id=name,
                           source_row_witness={'kind': 'excel_rows_v1', 'snapshot': 'a' * 64, 'rows': rows})
    observation['provenance'] = {
        'source': {'source_id': 'book', 'schema': 'public', 'table': '科目'},
        'source_key_columns': ['子科目' if rewritten else '父科目'],
        'target_key_columns': ['父科目' if rewritten else '子科目'],
        'predicate': {'column': '在用', 'op': 'eq', 'value': 1},
    }
    if rewritten:
        observation['endpoint_origins'] = [
            {'edge_id': f'old-{i:02}', 'type': 'member', 'source': f'child-{i:02}', 'target': 'old-parent'}
            for i in range(40)]
    return observation


def _index(tmp_path, rows):
    graph = {
        'support_visibility_version': 1,
        'nodes': [{'id': name, 'type': '指标', 'identity': {'code': name},
                   'supports': [_support(name, _context('global'))]} for name in ('parent', 'child')],
        'edges': [
            {'id': 'parent-child', 'type': 'contains', 'source': 'parent', 'target': 'child',
             'supports': [_observation('forward-observation', [[6, 6]])]},
            {'id': 'child-parent', 'type': 'member', 'source': 'child', 'target': 'parent',
             'supports': [_observation('private-observation', [[8, 8]], scope='private'),
                          _observation('reverse-observation', rows, rewritten=True)]},
        ],
        'definitions': {'entities': [], 'relations': []},
    }
    sync_graph_to_retrieval(graph, tmp_path)
    return SubgraphRetriever(tmp_path)


def _incoming(retriever, mode):
    result = RetrieveSubgraphRequest(mode=mode, seed_ids=['parent'], target_id='child', direction='incoming',
                                     relation_types=['member'], max_depth=1).run(retriever, ['public'])
    path, = result[mode]['paths']
    assert path['node_ids'] == ['parent', 'child'] and path['edge_ids'] == ['child-parent']
    assert path['observed_path'] is False
    assert 'private-observation' not in canonical(result)
    edge, = result['edges']
    observation, = edge['observations']
    return observation


@pytest.mark.parametrize('mode', ['drilldown', 'traverse'])
def test_rewritten_edge_keeps_its_own_worksheet_rows_in_the_path(tmp_path, mode):
    rows = [[969, 969], [972, 974]]
    retriever = _index(tmp_path, rows)
    observation = _incoming(retriever, mode)
    assert observation['projection'] == 'source_binding'
    assert observation['observation_id'] == 'reverse-observation'
    assert observation['source_row_witness'] == {'kind': 'excel_rows_v1', 'snapshot': 'a' * 64, 'rows': rows}
    assert observation['provenance'] == _observation('', rows, rewritten=True)['provenance']
    assert 'endpoint_origins' not in observation
    assert len(canonical(observation).encode('utf-8')) <= 2048


def test_whole_row_set_stays_available_by_observation_id_when_it_exceeds_the_path_budget(tmp_path):
    rows = [[i, i] for i in range(10, 1000, 2)]
    retriever = _index(tmp_path, rows)
    preview = _incoming(retriever, 'traverse')
    assert preview['projection'] == 'source_binding'
    assert 'source_row_witness' not in preview
    assert len(canonical(preview).encode('utf-8')) <= 2048
    result = RetrieveSubgraphRequest(mode='search', node_id='parent', hops=1).run(retriever, ['public'])
    reverse, = [edge for edge in result['edges'] if edge['id'] == 'child-parent']
    full, = reverse['observations']
    assert full['observation_id'] == preview['observation_id'] == 'reverse-observation'
    assert full['source_row_witness']['rows'] == rows


def test_worksheet_rows_do_not_displace_a_verified_source_identity():
    rows = [[i, i] for i in range(1, 170, 2)]
    witness = _observation('key-observation', rows, rewritten=True)
    witness['source_identity'] = {'values': ['科目' * 160], 'value_types': ['text']}
    preview = evidence_preview(witness)
    assert preview['projection'] == 'source_binding'
    assert preview['source_identity'] == witness['source_identity']
    assert 'source_row_witness' not in preview
    both = {**preview, 'source_row_witness': witness['source_row_witness']}
    assert len(canonical(preview).encode('utf-8')) <= 2048 < len(canonical(both).encode('utf-8'))
    without_key = {k: v for k, v in witness.items() if k != 'source_identity'}
    assert evidence_preview(without_key)['source_row_witness'] == witness['source_row_witness']


@pytest.mark.parametrize('missing', ['valid_range', 'source_id', 'schema', 'table'])
def test_incomplete_worksheet_location_does_not_claim_a_row(missing):
    witness = _observation('partial-observation', [[969, 969]], rewritten=True)
    if missing == 'valid_range':
        witness['source_row_witness']['rows'] = [[969, 968]]
    else:
        del witness['provenance']['source'][missing]
    preview = evidence_preview(witness)
    assert preview['observation_id'] == 'partial-observation'
    assert preview['projection'] == ('reference_only' if missing == 'table' else 'source_binding')
    assert 'source_row_witness' not in preview
