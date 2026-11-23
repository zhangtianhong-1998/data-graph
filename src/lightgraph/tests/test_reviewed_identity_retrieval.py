"""A reviewed object retains the typed local keys that bind facts in each source."""
import pytest

from lightgraph.construction.executor import execute_draft
from lightgraph.sources import MemorySource
from lightgraph.tests.test_retrieval_identity_and_row_proofs import entity, relation, table
from lightgraph.tests.test_web_graph_review import client
from lightgraph.tests.test_web_publication_identity import GRAPH, ROOT, listing, publish, submit


def contribution(name, variant):
    if name == 'east':
        keys, names, values = ['legacy_code'], ['area_code'], ['E17']
    elif variant == 'same_value':
        keys, names, values = ['local_id'], ['区'], ['E17']
    else:
        # The two physical values look the same when printed. Their order and types matter.
        keys, names, values = ['division', 'district'], ['社', '区'], ['10', 10]
    cols = [*keys, 'alias', 'label', 'metric', 'fact', 'project']
    spec = table(name, cols)
    mapping = {'source_id': 's', 'table': name, 'context_columns': cols,
               'business_context': {'status': 'scoped', 'columns': {'project': 'project'}}}
    area = entity('地区' + name, mapping, keys, keys=names, label='label')
    area['mappings'][0]['alias_columns'] = ['alias']
    draft = {'draft_id': name, 'entities': [area, entity('指标', mapping, ['metric']), entity('订单', mapping, ['fact'])],
             'relations': [relation('归属地区', '订单', area['type'], mapping, ['fact'], keys),
                           relation('计算指标', '订单', '指标', mapping, ['fact'], ['metric'])]}
    rows = {spec.qualified_name: [(*values, '别名' + name, '华东', '指标' + name, '订单' + name, 'M')]}
    with MemorySource([spec], rows) as source:
        built = execute_draft(draft, [source], source.describe())
    assert built['passed'], built['diagnostics']
    graph = built['graph']
    if variant == 'unbound':
        region = next(n for n in graph['nodes'] if n['type'] == area['type'])
        # A readable old snapshot may lack the physical binding that proves the local key.
        region['supports'][0]['provenance'].pop('key_columns')
        region['supports'][0].pop('row_context', None)
    if variant == 'reordered' and name == 'west':
        edge = next(e for e in graph['edges'] if e['type'] == '归属地区')
        edge['supports'][0]['provenance']['target_key_columns'].reverse()
    return {'contributor_id': name, 'kind': 'unit', 'graph': graph}


@pytest.mark.parametrize('variant', ['bound', 'unbound', 'reordered', 'same_value'])
def test_reviewed_local_key_query_uses_its_own_member_evidence(client, variant):
    publish(client, [contribution(n, variant) for n in ('east', 'west')])
    before = listing(client)
    candidate, = [g for g in before['candidates'] if set(n['kind'] for n in g['nodes']) == {'地区east', '地区west'}]
    response = submit(client, before, candidate)
    assert response.status_code == 200, response.text
    assert client.post(ROOT + '/retrieval/sync').status_code == 200
    exported = client.get(GRAPH + '/export').json()
    region, = [n for n in exported['entities'] if n['id'] in candidate['members']]
    ids = {n['name']: n['id'] for n in exported['entities']}

    def query(selector, metric, project='M'):
        response = client.post(ROOT + '/retrieval/query', json={'mode': 'dimensional', 'business_context': {'project': project},
            'conditions': [selector, {'node_id': ids['指标' + metric]}]})
        assert response.status_code == 200, response.text
        result = response.json()
        if result['dimensional']['fact_ids']:
            from lightgraph.retrieval.agent_context import agent_context
            local, = [b for b in agent_context(result)['bindings'] if b['ref'] == region['id'] and b.get('identity_reviewed')]
            assert local['source']['table'] == metric
            if metric == 'east' or variant == 'same_value':
                assert local['source_identity'] == {'values': ['E17'], 'value_types': ['text']}
            else:
                assert local['mapping']['key_columns'] == ['division', 'district']
                assert local['source_identity'] == {'values': ['10', 10], 'value_types': ['text', 'integer']}
        return result['dimensional']['fact_ids']

    for name, other in [('east', 'west'), ('west', 'east')]:
        valid = variant != 'unbound' and (variant != 'reordered' or name == 'east')
        expected = [ids['订单' + name]] if valid else []
        assert query({'node_id': region['id']}, name) == expected
        assert query({'text': '别名' + name}, name) == expected
        assert query({'text': '别名' + other}, name) == []
        assert query({'node_id': region['id']}, name, project='K') == []
    assert query({'text': 'E17'}, 'east') == ([] if variant == 'unbound' else [ids['订单east']])
    assert query({'text': 'E17'}, 'west') == ([ids['订单west']] if variant == 'same_value' else [])


def test_declared_order_binds_keys_without_a_retained_row_and_old_ambiguous_origins_stay_unbound():
    from copy import deepcopy
    from lightgraph.construction.human_identity import project
    from lightgraph.construction.merger import merge_construction_graphs
    from lightgraph.retrieval.sync import _observations
    inputs = [contribution(n, 'bound') for n in ('east', 'west')]
    graph = merge_construction_graphs(inputs)
    group, = [g for g in graph['candidate_identity_merges'] if g.get('normalized_label') == '华东']
    member = next(n for n in graph['nodes'] if n['type'] == '地区west')
    support, = member['supports']
    support.pop('row_context', None)
    decision = {'decision_id': 'confirmed', 'verdict': 'same', 'status': 'active', 'members': group['node_ids']}
    def projected():
        result = project(graph, [decision], 'baseline')
        node = next(n for n in result['nodes'] if n['id'] in group['node_ids'])
        return next(obs for obs in _observations(node) if obs['provenance']['source']['table'] == 'west')
    assert projected()['source_identity'] == {'values': ['10', 10], 'value_types': ['text', 'integer']}
    support['original_key_values'] = {'division': '10', 'district': '10'}
    support['identity_origins'] = [
        {'identity': {'id': '10'}, 'identity_types': {'id': kind}} for kind in ('decimal', 'text')]
    before = deepcopy(support)
    assert 'source_identity' not in projected()
    assert before == support
