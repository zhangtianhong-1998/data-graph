"""Replay of source-backed entities across parallel branches and merge-worker deltas."""
from copy import deepcopy

import pytest

from lightgraph.construction.access import filter_graph_scopes
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.tests.test_cross_source_fusion import source_graph


def projection(kind='指标', key='report_code', *, project='M', table='catalog'):
    graph = source_graph(kind, key, 'DPC_OCF_94504', 'db', '代垫款', '值', 15)
    for node in graph['nodes']:
        node['identity_types'] = {key: 'text'}
        support = node['supports'][0]
        support['business_context'] = {'version': 1, 'status': 'scoped', 'dimensions': {'project': project}}
        support['provenance'].update(key_columns=['REPORT_CODE'], label_column='REPORT_NAME')
        support['provenance']['source']['table'] = table
    support = graph['edges'][0]['supports'][0]
    support['business_context'] = graph['nodes'][0]['supports'][0]['business_context']
    support['provenance'].update(source={'source_id': 'db', 'schema': 'dm', 'table': table},
                                 source_key_columns=['PARENT_CODE'], target_key_columns=['CHILD_CODE'])
    return graph


def unit(name, graph, kind='unit', **options):
    return {'contributor_id': name, 'kind': kind, 'graph': graph, **options}


def test_alias_replay_combines_typed_source_facts_and_rewires_edges():
    a = unit('A_C', projection())
    b = unit('B_C', projection('经营指标', '指标编码'))
    graphs = [a, b]
    before = deepcopy(graphs)
    result = merge_construction_graphs(graphs)
    assert result == merge_construction_graphs([b, a])
    assert graphs == before
    assert len(result['nodes']) == 2 and len(result['edges']) == 1
    assert len(result['definitions']['entities']) == 1
    assert result['candidate_identity_merges'] == []
    edge, = result['edges']
    node = next(n for n in result['nodes'] if n['id'] == edge['source'])
    assert node['type'] == result['definitions']['relations'][0]['source_type']
    support, = node['supports']
    assert {e['contributor_id'] for e in support['execution_origins']} == {'A_C', 'B_C'}
    assert {e['type'] for e in support['identity_origins']} == {'指标', '经营指标'}
    assert support['original_key_values'] == {'REPORT_CODE': 'DPC_OCF_94504'}
    assert len(filter_graph_scopes(result, ['public'], business_context={'project': 'M'})['nodes']) == 2
    assert len(filter_graph_scopes(result, ['public'], business_context={'project': 'K'})['nodes']) == 0

    delta = projection('经营指标', '指标编码')
    delta['nodes'][0]['supports'][0].update(label='代垫款（旧称）', properties={'值': [20]})
    staged = merge_construction_graphs([unit('up', result, 'base', local_source_namespace=True), unit('merge', delta)])
    assert {n['id'] for n in staged['nodes']} == {n['id'] for n in result['nodes']}
    node = next(n for n in filter_graph_scopes(staged, ['public'])['nodes'] if n['id'] == edge['source'])
    assert node['properties'] == {'值': [15, 20]}
    assert sorted(node['label_variants']) == ['代垫款', '代垫款（旧称）']
    replay = merge_construction_graphs([unit('up', staged, 'base', local_source_namespace=True), unit('merge', delta)])
    assert replay['nodes'] == staged['nodes']
    assert replay['edges'] == staged['edges']
    republished = merge_construction_graphs([unit('final', staged, 'merged')])
    assert len(republished['nodes']) == 2 and len(republished['edges']) == 1
    assert republished['conflict_isolation']['nodes'] == 0


@pytest.mark.parametrize('difference', ['project', 'table', 'level', 'prefix', 'type', 'predicate',
                                         'role', 'missing_role', 'private', 'foreign_base', 'inherited', 'independent'])
def test_ambiguous_observations_and_other_contexts_stay_separate(difference):
    a = unit('ac', projection())
    b = unit('bc', projection('经营指标', '指标编码'))
    graph = b['graph']
    for node in graph['nodes']:
        support = node['supports'][0]
        prov = support['provenance']
        if difference == 'project':
            support['business_context']['dimensions']['project'] = 'K'
        elif difference == 'table':
            prov['source']['table'] = 'other'
        elif difference == 'level':
            prov['key_columns'] = ['LEVEL2_CODE']
        elif difference == 'prefix':
            node['identity']['指标编码'] = 'OTHER_' + node['identity']['指标编码']
        elif difference == 'type':
            node['identity']['指标编码'] = 94504
            node['identity_types']['指标编码'] = 'integer'
        elif difference == 'predicate':
            prov['predicate'] = {'op': 'ne', 'column': 'status', 'value': 'archived'}
        elif difference == 'private':
            support['scope_ref'] = 'private'
        elif difference == 'inherited':
            support['inherited_from'] = {'inherit_id': 'foreign'}
    if difference == 'missing_role':
        graph['edges'] = []
    if difference == 'role':
        graph['edges'][0]['supports'][0]['provenance']['source_key_columns'] = ['OTHER_PARENT_CODE']
    if difference == 'foreign_base':
        b['kind'] = 'base'
    merged = merge_construction_graphs([a, b], independent_outputs=difference == 'independent')
    assert len(merged['nodes']) == 4


def test_overlapping_pairs_share_c_but_do_not_treat_a_b_as_one_source():
    graphs = []
    for label in ['A', 'B']:
        graph = projection()
        for node in graph['nodes']:
            additional = deepcopy(node['supports'][0])
            additional['provenance']['source']['table'] = label
            additional['properties'] = {'归属': [label]}
            node['supports'].append(additional)
        graphs.append(unit(label + 'C', graph))
    result = merge_construction_graphs(graphs)
    assert len(result['nodes']) == 2
    for node in filter_graph_scopes(result, ['public'])['nodes']:
        assert node['properties']['归属'] == ['A', 'B']
        assert {s['provenance']['source']['table'] for s in node['supports']} == {'catalog', 'A', 'B'}

    # The chain A - (A,B) - B has no single shared identity witness.
    for node in graphs[0]['graph']['nodes']:
        node['supports'] = node['supports'][1:]
    for node in graphs[1]['graph']['nodes']:
        node['supports'][0]['provenance']['source']['table'] = 'A'
    result = merge_construction_graphs([*graphs, unit('B_only', projection(table='B'))])
    assert len(result['nodes']) == 6


def test_derived_replay_refuses_foreign_records_inside_a_local_workflow_base():
    external = projection()
    for node in external['nodes']:
        node['supports'][0]['inherited_from'] = {'inherit_id': 'outside', 'source_project_id': 'P2'}
    base = merge_construction_graphs([unit('external', external, 'base')])
    result = merge_construction_graphs([unit('upstream', base, 'base', local_source_namespace=True),
                                        unit('worker', projection())])
    assert len(result['nodes']) == 4


def test_two_real_publications_reuse_observations_and_view_counts_authorized_groups(tmp_path):
    from lightgraph.construction.tools import ConstructionTools
    from lightgraph.construction.workspace import dump_yaml
    from lightgraph.tests.test_construction_tools import config_for, draft
    from lightgraph.web.construction_graph import graph_page

    config = config_for(tmp_path)
    graphs = []
    for contributor, key in [('AC', '编码'), ('BC', '设备编码')]:
        tools = ConstructionTools(config, tmp_path / contributor)
        try:
            table = tools.call('describe_sources')['tables'][0]
            authored = draft(table)
            authored['entities'][0]['key_names'] = [key]
            mapping = authored['entities'][0]['mappings'][0]
            mapping['business_context'] = {'status': 'scoped', 'values': {'project': 'M'}}
            assert tools.call('save_graph_draft', yaml_text=dump_yaml(authored))['status'] == 'ok'
            assert tools.call('validate_graph_draft', draft_id='设备')['passed']
            assert tools.call('publish_graph_draft', draft_id='设备')['status'] == 'ok'
            graphs.append(unit(contributor, tools.workspace.read_published_graph()))
        finally:
            tools.close()
    merged = merge_construction_graphs(graphs)
    assert [len(g['graph']['nodes']) for g in graphs] == [2, 2]
    assert len(merged['nodes']) == 2
    for project, count in [('M', 2), ('K', 0)]:
        page = graph_page(config, prepared_graph=merged, business_context={'project': project})
        assert page['total_nodes'] == page['merged_observation_nodes'] == count
    assert filter_graph_scopes(merged, [])['merged_observation_nodes'] == 0


def test_incomplete_witness_on_one_node_keeps_that_pair_separate():
    a, b = unit('A', projection()), unit('B', projection())
    del b['graph']['nodes'][1]['supports'][0]['provenance']['key_columns']
    merged = merge_construction_graphs([a, b])
    assert len(merged['nodes']) == 3
    assert len(merged['edges']) == 2
    assert len([n for n in merged['nodes'] if n['identity']['report_code'] == 'DPC_OCF_94504']) == 1
    assert len(merged['candidate_identity_merges']) == 1
    assert len(merge_construction_graphs([a, b], recognize_source_replays=False)['nodes']) == 4


def test_undeclared_projects_require_identical_source_and_row_evidence():
    def unknown(label, extra):
        graph = projection()
        for node in graph['nodes']:
            common = node['supports'][0]
            common.pop('business_context')
            additional = deepcopy(common)
            additional['provenance']['source']['table'] = extra
            additional['provenance']['property_columns'] = {'归属': 'project'}
            additional['properties'] = {'归属': [label]}
            node['supports'].append(additional)
        return graph

    a, b = unknown('M', 'A'), unknown('K', 'B')
    assert len(merge_construction_graphs([unit('ac', a), unit('bc', b)])['nodes']) == 4
    # The same key table can encode an undeclared scope in a non-key attribute.
    b = unknown('K', 'A')
    assert len(merge_construction_graphs([unit('ac', a), unit('bc', b)])['nodes']) == 4
    assert len(merge_construction_graphs([unit('ac', a), unit('bc', a)])['nodes']) == 2


def test_matching_source_keys_without_alias_role_remain_candidates_when_names_disagree():
    a, b = unit('A', projection()), unit('B', projection('独立类型', '编号'))
    b['graph']['edges'] = []
    for node in b['graph']['nodes']:
        node['supports'][0]['label'] = '另一名称' + node['supports'][0]['label']
    merged = merge_construction_graphs([a, b])
    assert len(merged['nodes']) == 4
    assert len(merged['candidate_identity_merges']) == 2
    assert {candidate['reason'] for candidate in merged['candidate_identity_merges']} == {'source_key_requires_role_review'}
