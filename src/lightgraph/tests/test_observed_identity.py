"""Replay of source-backed entities across parallel branches and merge-worker deltas."""
from copy import deepcopy

import pytest

from lightgraph.construction.access import filter_graph_scopes
from lightgraph.construction.merger import identity_candidate_node_counts, merge_accounting, merge_construction_graphs
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


def test_legacy_candidate_without_member_ids_omits_unknown_counts():
    assert identity_candidate_node_counts({'candidate_identity_merges': [{'label': '旧候选'}]}) == {}


def test_overlapping_identity_candidates_count_members_once_in_run_and_publication(tmp_path):
    from lightgraph.construction.workflow import publish_premerged_graph
    from lightgraph.construction.workspace import read_internal_yaml
    from lightgraph.tests.test_construction_tools import config_for

    contributors = []
    for source, kind in [('a', 'Catalogue'), ('b', 'Catalogue'), ('c', 'Collection')]:
        graph = source_graph(kind, 'code', '01', source, '同名对象', 'note', source)
        graph['nodes'] = graph['nodes'][:1]
        graph['edges'] = []
        contributors.append(unit(source, graph))
    merged = merge_construction_graphs(contributors)
    assert sorted(len(c['node_ids']) for c in merged['candidate_identity_merges']) == [2, 3]
    assert all(c['status'] == 'candidate_only' for c in merged['candidate_identity_merges'])
    config = config_for(tmp_path)
    output = tmp_path / 'publication'
    publish_premerged_graph(output, config, merged=merged, included_units=['a', 'b', 'c'],
                            skipped_units=[], unit_attempts={})
    summary = read_internal_yaml(output / 'construction' / 'workflow-merge-summary.yaml')
    assert summary['nodes'] == 3 and summary['candidate_identity_merges'] == 2
    assert summary['semantic_status'] == 'pending_alignment'
    assert summary['merge_counts'] == merged['fusion_diagnostics']
    for record in (summary, merge_accounting(contributors, merged)):
        assert record['candidate_identity_nodes'] == 3
        assert record['candidate_identity_nodes_in_multiple_groups'] == 2


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
    assert merge_accounting(graphs, result) == {
        'input_nodes': 4, 'input_edges': 2, 'output_nodes': 2, 'output_edges': 1,
        'candidate_identity_groups': 0, 'quarantined_nodes': 0, 'quarantined_edges': 0,
        'candidate_identity_nodes': 0, 'candidate_identity_nodes_in_multiple_groups': 0,
        'unresolved_relations': 0,
        'source_replay_status': 'ran', 'explicit_entity_rules': 0, 'explicit_rule_preparation_ran': False,
        'source_replay_matched_inputs': 4, 'source_replay_groups': 2, 'source_replay_collapsed_nodes': 2,
        'dictionary_eligible_inputs': 0, 'dictionary_groups_without_observed_member': 0,
        'observed_dictionary_collapsed_nodes': 0,
        'collapsed_nodes_before_quarantine': 2, 'input_physical_column_nodes': 0,
        'output_physical_column_nodes': 0, 'source_replay_considered_row_nodes': 4,
        'source_replay_eligible_row_nodes': 4, 'source_replay_cross_contributor_source_witness_groups': 2,
    }
    assert 'fusion_diagnostics' not in filter_graph_scopes(result, [])
    assert merge_construction_graphs(graphs, independent_outputs=True)['fusion_diagnostics']['source_replay_status'] == 'independent_outputs'
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
    assert republished['fusion_diagnostics']['source_replay_status'] == 'insufficient_contributors'
    assert 'source_replay_eligible_row_nodes' not in republished['fusion_diagnostics']
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


def test_draft_replay_reuses_endpoint_across_independent_relationship_projections():
    from lightgraph.construction.workspace import _merge_graphs
    from lightgraph.runtime.config import FusionPolicyConfig

    drafts = []
    for key, link in [('artifact_id', 'catalog_ref'), ('object_id', 'box_ref'), ('registration', 'epoch_ref')]:
        graph = projection('Artifact', key, table='collection')
        edge = graph['edges'][0]
        graph['definitions']['relations'][0]['type'] = edge['type'] = link
        edge['id'] = link + '_edge'
        if key == 'registration':
            graph['nodes'][0]['id'] = edge['source'] = 'alternate-local-id'
        # Each new relation attaches to the same physical source end.
        edge['supports'][0]['provenance'].update(source_key_columns=['CATALOG_ID'], target_key_columns=[link])
        graph['draft_id'] = link
        drafts.append(graph)
    originals = deepcopy(drafts)
    result = _merge_graphs(drafts, fusion_policy=FusionPolicyConfig())
    assert drafts == originals
    assert len(result['nodes']) == 2 and len(result['edges']) == 3
    nodes = {n['id']: n for n in result['nodes']}
    assert all(e['source'] in nodes and e['target'] in nodes for e in result['edges'])
    assert len({e['source'] for e in result['edges']}) == len({e['target'] for e in result['edges']}) == 1
    assert len(result['definitions']['entities']) == 1
    assert len({e['type'] for e in result['edges']}) == 3
    node = nodes[result['edges'][0]['source']]
    keys = {tuple(origin['identity']) for s in node['supports'] for origin in s['identity_origins']}
    assert keys == {('artifact_id',), ('object_id',), ('registration',)}
    assert len(filter_graph_scopes(result, ['public'], business_context={'project': 'K'})['nodes']) == 0


@pytest.mark.parametrize('boundary', ['table', 'project', 'scope', 'predicate', 'role'])
def test_distinct_draft_identities_do_not_widen_observed_scope(boundary):
    from lightgraph.construction.workspace import _merge_graphs

    a = projection('Artifact', 'key', table='specimens')
    b = projection('CollectionObject', 'registry_key', table='specimens')
    for node, end in zip(b['nodes'], ('source', 'target'), strict=True):
        node['id'] = b['edges'][0][end] = 'other_' + node['id']
    b['edges'][0]['id'] = 'other_edge'
    for n in b['nodes']:
        s = n['supports'][0]
        if boundary == 'table':
            s['provenance']['source']['table'] = 'unrelated'
        elif boundary == 'project':
            s['business_context']['dimensions']['project'] = 'K'
        elif boundary == 'scope':
            s['scope_ref'] = 'restricted'
        elif boundary == 'predicate':
            s['provenance']['predicate'] = {'op': 'ne', 'column': 'era', 'value': 'archived'}
    if boundary == 'role':
        b['edges'][0]['supports'][0]['provenance'].update(source_key_columns=['FROM_B'], target_key_columns=['TO_B'])
    result = _merge_graphs([a, b])
    assert len(result['nodes']) == 4
    assert len(result['edges']) == 2
    assert len({e[end] for e in result['edges'] for end in ('source', 'target')}) == 4
    reverse = _merge_graphs([b, a])
    assert {n['id'] for n in reverse['nodes']} == {n['id'] for n in result['nodes']}


def test_independent_drafts_reuse_the_validated_source_key_at_publication(tmp_path):
    from lightgraph.construction.tools import ConstructionTools
    from lightgraph.construction.workspace import dump_yaml
    from lightgraph.tests.test_construction_tools import config_for, draft

    tools = ConstructionTools(config_for(tmp_path), tmp_path / 'publication')
    try:
        source = tools.describe_sources()['tables'][0]
        for key in ['accession_number', 'record_code', 'specimen_key']:
            proposal = draft(source, draft_id=key, kind='Specimen')
            proposal['entities'][0]['key_names'] = [key]
            assert tools.call('save_graph_draft', yaml_text=dump_yaml(proposal))['status'] == 'ok'
            assert tools.call('validate_graph_draft', draft_id=key)['passed']
            assert tools.call('publish_graph_draft', draft_id=key)['status'] == 'ok'
        published = tools.workspace.read_published_graph()
        assert len(published['definitions']['entities']) == 1
        assert len(published['nodes']) == 2
        for node in published['nodes']:
            support, = node['supports']
            assert {r['draft_id'] for r in support['execution_origins']} == {'accession_number', 'record_code', 'specimen_key'}
            assert len(support['identity_origins']) == 3
    finally:
        tools.close()


def test_quarantine_follows_rewritten_id_and_keeps_draft_counterexamples():
    from lightgraph.construction.workspace import _merge_graphs

    a = projection('Specimen', 'accession')
    b = projection('Specimen', 'other_code')
    a['draft_id'], b['draft_id'] = 'one', 'two'
    # Authored logical IDs can differ while the typed physical key and role agree.
    b['nodes'][0]['id'] = b['edges'][0]['source'] = 'new-source'
    b['nodes'][0]['supports'][0]['label'] = 'Disputed label'
    result = _merge_graphs([a, b], isolate_conflicts=True)
    assert len(result['nodes']) == 1 and not result['edges']
    assert result['conflict_isolation']['nodes'] == 1
    assert result['conflict_isolation']['draft_node_ids'] == {'one': ['db_1'], 'two': ['new-source']}
