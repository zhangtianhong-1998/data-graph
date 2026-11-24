from copy import deepcopy

import pytest
from pydantic import ValidationError

from lightgraph.construction.fusion import prepare_graph
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import dump_yaml
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import FusionPolicyConfig
from lightgraph.tests.test_cross_source_fusion import source_graph
from lightgraph.tests.test_publication_identity_authority import _config, _draft, _source


def binding(table):
    return {'source_id': 'db', 'schema': 'dm', 'table': table,
            'key_columns': {'org': 'owner_col', 'sku': 'product_col'}}


def rule(*tables):
    result = {'entity_type': 'Item', 'canonical_type': 'Product', 'identity_domain': 'catalog',
              'key_mapping': {'code': 'sku', 'owner': 'org'}}
    if tables:
        result['source_bindings'] = [binding(t) for t in tables]
    return result


def graph(table):
    g = source_graph('Item', 'sku', 'A', 'db', 'Product A', table, table)
    g['definitions']['entities'][0]['key_names'] = ['sku', 'org']
    for n in g['nodes']:
        n['identity'] = {'org': 'M', 'sku': n['identity']['sku']}
        prov = n['supports'][0]['provenance']
        prov.update(source={'source_id': 'db', 'schema': 'dm', 'table': table}, key_columns=['product_col', 'owner_col'])
    return g


def test_unrestricted_policy_retains_historical_serialization():
    expected = {**rule(), 'key_normalizers': {}, 'property_aliases': {}, 'property_normalizers': {}}
    p = FusionPolicyConfig(entity_rules=[{**rule(), 'source_bindings': []}])
    assert p.model_dump(mode='json')['entity_rules'] == [expected]
    assert p.entity_rules[0].model_dump() == expected
    with pytest.raises(ValidationError):
        FusionPolicyConfig(entity_rules=[{**rule(), 'source_bindings': [{**binding('a'), 'key_columns': {'sku': 'c'}}]}])


def test_tables_must_independently_observe_the_approved_physical_key():
    policy = {'fusion_policy': {'entity_rules': [rule('base', 'sales')]}}
    inputs = [{'contributor_id': t, 'source_namespace': 'here', 'graph': graph(t)} for t in ('base', 'sales')]
    result = merge_construction_graphs(inputs, merge_policy=policy)
    assert len(result['nodes']) == 2 and len(result['edges']) == 1
    assert all(set(n['properties']) == {'base', 'sales'} for n in result['nodes'])
    third = {'contributor_id': 'delta', 'source_namespace': 'here', 'graph': graph('unknown')}
    with pytest.raises(LightGraphError, match='fusion_source_binding_unverified'):
        merge_construction_graphs([*inputs, third], merge_policy=policy)
    frozen = {'kind': 'base', 'contributor_id': 'base', 'source_namespace': 'here', 'graph': result}
    expanded = {'fusion_policy': {'entity_rules': [rule('base', 'sales', 'unknown')]}}
    attached = merge_construction_graphs([frozen, third], merge_policy=expanded)
    assert [n['id'] for n in attached['nodes']] == [n['id'] for n in result['nodes']]
    assert all(set(n['properties']) == {'base', 'sales', 'unknown'} for n in attached['nodes'])
    with pytest.raises(LightGraphError, match='fusion_source_binding_unverified'):
        merge_construction_graphs([frozen, {**third, 'source_namespace': 'other'}], merge_policy=expanded)
    legacy = deepcopy(frozen)
    legacy['graph'].pop('physical_source_namespace_binding')
    with pytest.raises(LightGraphError, match='fusion_source_binding_unverified'):
        merge_construction_graphs([legacy, third], merge_policy=expanded)
    legacy['local_source_namespace'] = True
    assert len(merge_construction_graphs([legacy, third], merge_policy=expanded)['nodes']) == 2


@pytest.mark.parametrize('issue', ['wrong_order', 'missing', 'foreign', 'other_schema', 'other_source', 'stale_origins'])
def test_asserting_the_same_type_and_code_does_not_grant_table_authority(issue):
    g = graph('base')
    support = g['nodes'][0]['supports'][0]
    prov = support['provenance']
    if issue == 'wrong_order':
        prov['key_columns'].reverse()
    elif issue == 'missing':
        prov.pop('key_columns')
    elif issue == 'foreign':
        support['inherited_from'] = 'foreign/project'
    elif issue == 'other_schema':
        prov['source']['schema'] = 'other'
    elif issue == 'other_source':
        prov['source']['source_id'] = 'elsewhere'
    else:
        support['identity_origins'] = [{'id': 'historical', 'type': 'Earlier', 'identity': {'key': 1}}]
    with pytest.raises(LightGraphError, match='fusion_source_binding_unverified'):
        prepare_graph(g, {'entity_rules': [rule('base')]})


def test_a_prepared_or_legacy_base_must_prove_each_source_before_accepting_a_restricted_rule():
    bound = {'entity_rules': [rule('base')]}
    g = graph('base')
    prepared = prepare_graph(g, bound)
    assert prepare_graph(prepared, bound)['nodes'] == prepared['nodes']
    legacy = prepare_graph(g, {'entity_rules': [rule()]})
    assert prepare_graph(legacy, bound)['nodes'] == legacy['nodes']
    for record in (prepared, legacy):
        changed = deepcopy(record)
        changed['nodes'][0]['supports'][0]['provenance']['source']['table'] = 'not_approved'
        with pytest.raises(LightGraphError, match='fusion_source_binding_unverified'):
            prepare_graph(changed, bound)
        del changed['nodes'][0]['supports'][0]['identity_origins']
        with pytest.raises(LightGraphError, match='fusion_source_binding_unverified'):
            prepare_graph(changed, bound)
    g['nodes'][0]['identity']['org'] = g['nodes'][0]['identity']['sku']
    ambiguous_legacy = prepare_graph(g, {'entity_rules': [rule()]})
    with pytest.raises(LightGraphError, match='fusion_source_binding_unverified'):
        prepare_graph(ambiguous_legacy, bound)
    exact = prepare_graph(g, bound)
    assert prepare_graph(exact, bound)['nodes'] == exact['nodes']


def test_single_and_mixed_table_drafts_enforce_restricted_rules_before_publication(tmp_path):
    allowed = {'source_id': 'db', 'schema': 'public', 'table': 'east', 'key_columns': {'code': 'code'}}
    policy = FusionPolicyConfig(entity_rules=[{'entity_type': 'Asset', 'canonical_type': 'Asset',
        'identity_domain': 'asset_catalog', 'key_mapping': {'code': 'code'}, 'source_bindings': [allowed]}])
    with _source() as source:
        tools = ConstructionTools(_config().model_copy(update={'fusion_policy': policy}), tmp_path, adapters=[source])
        try:
            for table, mix, passed in [('east', False, True), ('west', False, False), ('east', True, False)]:
                draft = _draft(table)
                if mix:
                    draft['draft_id'] = 'mixed'
                    mappings = draft['entities'][0]['mappings']
                    mappings.append({**mappings[0], 'table': 'west'})
                assert tools.call('save_graph_draft', yaml_text=dump_yaml(draft))['status'] == 'ok'
                checked = tools.call('validate_graph_draft', draft_id=draft['draft_id'])
                assert checked['passed'] is passed
                if passed:
                    assert tools.call('publish_graph_draft', draft_id=draft['draft_id'])['status'] == 'ok'
                else:
                    assert any(d['code'] == 'fusion_source_binding_unverified' for d in checked['diagnostics'])
                    assert tools.call('publish_graph_draft', draft_id=draft['draft_id'])['status'] == 'error'
        finally:
            tools.close()
