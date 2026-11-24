"""Attribute rules apply to their input type and complete key, including on frozen publications."""
from copy import deepcopy
import json

import pytest

from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.errors import LightGraphError
from lightgraph.tests.test_cross_source_fusion import source_graph


def case(buyer_type='BuyerProduct', buyer_key='code'):
    def contributor(source, kind, key, field, value):
        raw = source_graph(kind, key, 'A', source, '产品', field, value)
        # Independent publications come from serialized checkpoints.
        return {'contributor_id': source, 'graph': json.loads(json.dumps(raw))}

    rules = [{'entity_type': kind, 'canonical_type': 'Product', 'identity_domain': 'catalog',
              'key_mapping': {'code': key}} for kind, key in [(buyer_type, buyer_key), ('SalesProduct', 'code')]]
    inputs = [contributor('buyer', buyer_type, buyer_key, 'region', '采购地'),
              contributor('old_sales', 'SalesProduct', 'code', 'unit', '套'),
              contributor('new_sales', 'SalesProduct', 'code', 'region', '销售地')]
    base = merge_construction_graphs(inputs[:2], merge_policy={'fusion_policy': {'entity_rules': rules}})
    new_rules = deepcopy(rules)
    new_rules[1]['property_aliases'] = {'region': 'sales_region'}
    return inputs, {'kind': 'base', 'contributor_id': 'frozen', 'graph': base}, {'fusion_policy': {'entity_rules': new_rules}}


@pytest.mark.parametrize('buyer_type,buyer_key', [('BuyerProduct', 'code'), ('SalesProduct', 'sku')])
def test_incremental_alias_checks_only_observations_of_its_input_rule(buyer_type, buyer_key):
    inputs, frozen, policy = case(buyer_type, buyer_key)
    batch = merge_construction_graphs(inputs, merge_policy=policy)
    staged = merge_construction_graphs([frozen, inputs[-1]], merge_policy=policy)

    def values(graph):
        return {n['id']: n['properties'] for n in graph['nodes']}

    assert values(staged) == values(batch)
    assert set(values(staged)) == set(values(frozen['graph']))
    assert all(fields == {'region': ['采购地'], 'sales_region': ['销售地'], 'unit': ['套']}
               for fields in values(staged).values())


@pytest.mark.parametrize('missing_proof', ['no_origin', 'unknown_input', 'mixed_origins', 'unowned_value'])
def test_incremental_alias_with_incomplete_ownership_still_requires_originals(missing_proof):
    inputs, frozen, policy = case()
    node = frozen['graph']['nodes'][0]
    buyer = next(s for s in node['supports'] if 'region' in s['properties'])
    if missing_proof == 'no_origin':
        del buyer['identity_origins']
    elif missing_proof == 'unknown_input':
        buyer['identity_origins'][0]['identity'] = {'wrong_key': 'A'}
    elif missing_proof == 'mixed_origins':
        buyer['identity_origins'].append({'id': 'sales', 'type': 'SalesProduct', 'identity': {'code': 'A'}})
    else:
        node['properties']['region'].append('只有汇总属性记录此值')
    with pytest.raises(LightGraphError, match='fusion_policy_requires_original_contributions'):
        merge_construction_graphs([frozen, inputs[-1]], merge_policy=policy)
