import pytest

from lightgraph.construction.graph_storage import _graph_digest
from lightgraph.construction.hierarchy_observations import observation_properties
from lightgraph.construction.hierarchy_replay import _compatible_remaps
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.construction.models import EntityMapping
from lightgraph.construction.reviewed_keys import reviewed_observation_digest
from lightgraph.runtime.config import MergePolicyConfig
from lightgraph.tests.test_cross_source_fusion import source_graph


def _inputs():
    inputs = []
    for unit, table in [('west', 'budget'), ('east', 'actual')]:
        graph = source_graph('District', 'code', 'W7', unit, 'West', table, table)
        for node in graph['nodes']:
            node['identity_types'] = {'code': 'text'}
            support = node['supports'][0]
            support['provenance'].update(source={'source_id': 'db', 'schema': 'dm', 'table': table},
                                         key_columns=['area_code'], label_column='area_name')
        if unit == 'west':
            graph['definitions']['entities'][0].update(identity_scope='scoped', identity_context_keys=['code'])
            for node in graph['nodes']:
                node['supports'][0]['business_context'] = {
                    'status': 'scoped', 'dimensions': dict(node['identity']), 'value_types': {'code': 'text'}}
        inputs.append({'contributor_id': unit, 'kind': 'unit', 'graph': graph})
    return inputs


def _policy(inputs):
    bindings = []
    for unit in inputs:
        source = unit['graph']['nodes'][0]['supports'][0]['provenance']['source']
        bindings.append({**source, 'contributor_id': unit['contributor_id'], 'graph_digest': _graph_digest(unit['graph']),
                         'key_column': 'area_code', 'name_column': 'area_name'})
    return {'reviewed_key_pairs': [{'entity_type': 'District', 'approval_ref': 'directory-review@17',
                                    'projections': bindings, 'observation_digests': [
                                        reviewed_observation_digest('text', 'W7', 'West')]}]}


def test_reviewed_key_pairs_replay_only_the_approved_observation_and_keep_both_edges():
    inputs = _inputs()
    policy = _policy(inputs)
    assert MergePolicyConfig.model_validate(policy).model_dump(mode='json')['reviewed_key_pairs'] == policy['reviewed_key_pairs']
    assert 'reviewed_key_pairs' not in MergePolicyConfig().model_dump(mode='json')
    baseline = merge_construction_graphs(inputs)
    graph = merge_construction_graphs(inputs, merge_policy=policy)
    assert graph == merge_construction_graphs(inputs[::-1], merge_policy=policy)
    assert len(graph['nodes']) == len(baseline['nodes']) - 1
    assert len(graph['edges']) == len(baseline['edges']) == 2
    district, = [n for n in graph['nodes'] if n['identity']['code'] == 'W7']
    assert {s['reviewed_input_node_id'] for s in district['supports']} == {'west_1', 'east_1'}
    assert all(s['identity_approval_ref'] == 'directory-review@17' for s in district['supports'])
    assert set(district['properties']) == {'actual', 'budget'}
    assert {e['source'] for e in graph['edges']} == {district['id']}
    by_owner = {s['contributor_id']: s for s in district['supports']}
    for edge in graph['edges']:
        for support in edge['supports']:
            witness = by_owner[support['contributor_id']]
            assert witness['identity_decision_members'] == support['identity_decision_endpoints']['source']
            assert witness['original_key_types'] == {'area_code': 'text'}
    assert len([n for n in graph['nodes'] if n['identity']['code'] == 'B']) == 2
    assert graph['fusion_diagnostics']['reviewed_key_pairs']['collapsed_nodes'] == 1
    inputs[0]['graph']['nodes'][0]['properties']['audit'] = [True]
    stale = merge_construction_graphs(inputs, merge_policy=policy)
    assert stale['fusion_diagnostics']['reviewed_key_pairs']['checkpoint_mismatch_rules'] == 1
    assert len(stale['nodes']) == len(baseline['nodes'])


@pytest.mark.parametrize('difference', ['name', 'key_column', 'scope', 'business_context'])
def test_pair_review_never_substitutes_for_authorized_observations(difference):
    inputs = _inputs()
    support = inputs[1]['graph']['nodes'][0]['supports'][0]
    if difference == 'name':
        support['label'] = None
    elif difference == 'scope':
        support['required_scope_refs'] = ['regional']
    elif difference == 'business_context':
        support['business_context'] = {'status': 'scoped', 'dimensions': {'owner': 'east'}, 'value_types': {'owner': 'text'}}
    else:
        support['provenance']['key_columns'] = ['other_axis']
    result = merge_construction_graphs(inputs, merge_policy=_policy(inputs))
    assert result['fusion_diagnostics']['reviewed_key_pairs']['collapsed_nodes'] == 0
    assert len(result['nodes']) == 4


def test_explicit_position_declaration_requires_physical_value_and_survives_serialization():
    mapping = EntityMapping(source_id='src', table='t', key_columns=['code'], constant_properties={'位置': 2},
                            hierarchy_position_roles={'位置': 'node'})
    support = {'properties': {'位置': [2]}, 'provenance': mapping.model_dump(mode='json')}
    assert observation_properties(support, ['code'], {'code': 2}) == '[[],{}]'
    assert observation_properties(support, ['code'], {'code': 1}) != '[[],{}]'
    del support['provenance']['hierarchy_position_roles']
    assert observation_properties(support, ['code'], {'code': 2}) != '[[],{}]'


def test_fold_cannot_close_a_two_step_path_of_an_acyclic_relation():
    graph = {'definitions': {'relations': [{'type': 'owns', 'source_type': 'X', 'target_type': 'X', 'acyclic': True}]},
             'edges': [{'id': 'r1', 'source': 'a', 'target': 'middle', 'type': 'owns'},
                       {'id': 'r2', 'source': 'middle', 'target': 'b', 'type': 'owns'}]}
    nodes = {i: {'type': 'X'} for i in ['a', 'b', 'middle']}
    assert _compatible_remaps(graph, {'a': ['a', 'b']}, nodes) == {}


def test_compile_from_committed_checkpoints_requires_approved_preview(tmp_path):
    import importlib.util
    import json
    import subprocess
    import sys
    from copy import deepcopy
    from pathlib import Path

    from lightgraph.construction.checkpoints import commit_checkpoint
    from lightgraph.construction.workflow import save_workflow_state

    entry = Path(__file__).resolve().parents[3] / 'scripts' / 'prepare_reviewed_key_pairs.py'
    spec = importlib.util.spec_from_file_location('prepare_reviewed_key_pairs', entry)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    run_id = 'run_' + 'a' * 24
    output = tmp_path / 'output'
    output.mkdir()
    inputs = _inputs()
    for item in inputs:
        unit = item['contributor_id']
        for node in item['graph']['nodes']:
            node['supports'][0]['execution_origins'] = [{'draft_id': unit, 'revision': 1},
                {'draft_id': unit, 'revision': 2, 'contributor_id': unit}]
        for foreign_level in ('support', 'origin'):
            other = deepcopy(item['graph']['nodes'][0])
            other.update(id=f'{unit}_{foreign_level}', identity={'code': foreign_level})
            support = other['supports'][0]
            if support.get('business_context'):
                support['business_context']['dimensions'] = dict(other['identity'])
            if unit == 'east':
                record = support if foreign_level == 'support' else support['execution_origins'][0]
                record['contributor_id'] = 'another_unit'
            item['graph']['nodes'].append(other)
    artifacts = {unit['contributor_id']: commit_checkpoint(output, run_id, unit['contributor_id'], unit,
                 input_digest='x', graph_kind='node_delta', status='completed_with_gaps') for unit in inputs}
    artifacts['merge_1'] = commit_checkpoint(output, run_id, 'merge_1', {'kind': 'merged',
        'graph': merge_construction_graphs(inputs, merge_policy={'fusion_policy': {}})}, input_digest='m',
        upstream_artifacts=[r['artifact_id'] for r in artifacts.values()])
    save_workflow_state(output, {'parent_run_id': run_id, 'artifacts': artifacts,
        'merge_nodes': {'merge_1': {'merge_mode': 'deterministic'}},
        'plan': {'merge_nodes': [{'node_id': 'merge_1'}], 'merge_policy': {}}})
    run_file = tmp_path / 'runs' / (run_id + '.json')
    run_file.parent.mkdir()
    run_file.write_text(json.dumps({'run_id': run_id, 'project_id': 'demo', 'output_path': 'output'}))
    rule, = _policy(inputs)['reviewed_key_pairs']
    selectors = [{'contributor_id': p['contributor_id'], 'source_id': p['source_id'], 'schema': p['schema'],
                  'table': p['table'], 'key_column': p['key_column'], 'name_column': p['name_column']}
                 for p in rule['projections']]
    criteria = {'entity_type': rule['entity_type'], 'projections': selectors, 'excluded_keys': ['B']}
    selectors_file = tmp_path / 'selectors.json'
    selectors_file.write_text(json.dumps(criteria), encoding='utf-8')
    prepare_cmd = [sys.executable, '-X', 'utf8', str(entry), str(run_file), '--node', 'merge_1',
                   '--spec', str(selectors_file)]
    preview_file = tmp_path / 'preview.json'
    subprocess.run([*prepare_cmd, '--output', str(preview_file)], check=True)
    preview = json.loads(preview_file.read_text(encoding='utf-8'))
    assert preview['observations'] == [{'key_type': 'text', 'key': 'W7', 'name': 'West'}]
    assert 'reviewed_key_pairs' not in preview
    decision = {'approval_ref': rule['approval_ref'], 'preview_digest': preview['preview_digest'],
                'observations': preview['observations']}
    compiled = script.prepare(run_file, 'merge_1', criteria, decision)
    assert compiled['reviewed_key_pairs'] == [rule]
    sim_file = tmp_path / 'compiled.json'
    subprocess.run([*prepare_cmd, '--simulate', '--output', str(sim_file)], check=True)
    simulated_only = json.loads(sim_file.read_text(encoding='utf-8'))
    assert simulated_only['mode'] == 'simulation_only' and 'reviewed_key_pairs' not in simulated_only
    assert simulated_only['simulated_count'] == 1 and 'approved_count' not in simulated_only
    from lightgraph.runtime.config import WorkflowConfig
    with pytest.raises(ValueError, match='simulated_key_pairs_cannot_be_published'):
        WorkflowConfig(merge_policy={'reviewed_key_pairs': simulated_only['simulated_key_pairs']})
    audit_spec = importlib.util.spec_from_file_location('audit_checkpoint_merge', entry.with_name('audit_checkpoint_merge.py'))
    audit = importlib.util.module_from_spec(audit_spec)
    audit_spec.loader.exec_module(audit)
    audited = subprocess.run([sys.executable, '-X', 'utf8', str(entry.with_name('audit_checkpoint_merge.py')),
                              str(run_file), '--node', 'merge_1', '--simulate-reviewed-key-pairs', str(sim_file),
                              '--include-edge-lineage'], check=True, stdout=subprocess.PIPE)
    report = json.loads(audited.stdout)
    assert report['with_source_replay']['rows_match_committed'] == {'nodes': True, 'edges': True}
    simulation = report['simulated_reviewed_key_pairs']
    assert simulation['node_reduction_vs_current_code_baseline'] == 1
    assert simulation['reviewed_membership']['incident_edges_with_reviewed_endpoints'] == 2
    assert simulation['reviewed_key_pairs']['collapsed_nodes'] == 1
    assert report['mode'] == 'simulated_reviewed_key_pairs' and report['model_calls'] == 0
    sim_file.write_text(json.dumps({**compiled, 'project_id': 'wrong'}))
    with pytest.raises(ValueError):
        audit.audit(run_file, 'merge_1', simulate_reviewed_key_pairs=sim_file)
    with pytest.raises(ValueError):
        script.prepare(run_file, 'merge_1', criteria, {**decision, 'preview_digest': 'old'})
    with pytest.raises(ValueError):
        script.prepare(run_file, 'merge_1', criteria, {**decision, 'observations': [
            {'key_type': 'text', 'key': 'B', 'name': 'B'}]})


def test_conflicting_qualified_properties_with_different_names_block_reviewed_fold():
    inputs = _inputs()
    for item, prop, value in zip(inputs, ('单位', 'unit_name'), ('件', '箱'), strict=True):
        s = item['graph']['nodes'][0]['supports'][0]
        s['properties'][prop] = [value]
        s['provenance']['identity_qualifiers'] = {prop: '计量单位'}
    result = merge_construction_graphs(inputs, merge_policy=_policy(inputs))
    assert result['fusion_diagnostics']['reviewed_key_pairs']['attribute_conflicts'] == 1
    assert len(result['nodes']) == 4


def test_reviewed_directory_batch_keeps_null_sentinels_other_axis_and_original_edges():
    """Two isolated business projections include 50 real codes and a null marker."""
    from copy import deepcopy

    inputs = _inputs()
    real_codes = [f'CODE{i:03}' for i in range(50)]
    names = {code: f'地区{i}' for i, code in enumerate(real_codes)}
    names['SNULL'] = '源为空'
    for item in inputs:
        unit, graph = item['contributor_id'], item['graph']
        node_template, edge_template = graph['nodes'][0], graph['edges'][0]
        graph['nodes'], graph['edges'] = [], []
        graph['definitions']['entities'].extend(
            {'type': kind, 'key_names': ['code']} for kind in ('Metric', 'OtherAxis'))
        relation = f'{unit}_metric_to_area'
        graph['definitions']['relations'][0].update(type=relation, source_type='Metric', target_type='District')

        def add_node(code, kind, key_column, label):
            node = deepcopy(node_template)
            node.update(id=f'{unit}_{kind}_{code}', type=kind, identity={'code': code}, label=label)
            support = node['supports'][0]
            support['label'] = label
            support['provenance'].update(definition=kind, key_columns=[key_column])
            if support.get('business_context'):
                support['business_context']['dimensions'] = {'code': code}
            graph['nodes'].append(node)
            return node['id']

        parents = [add_node(f'{unit}-metric-{i}', 'Metric', 'metric_key', f'{unit} metric {i}')
                   for i in range(2 if unit == 'west' else 1)]
        for i, (code, name) in enumerate(names.items()):
            area = add_node(code, 'District', 'area_code', name)
            if unit == 'west' and code in real_codes:
                add_node(code, 'OtherAxis', 'other_axis_code', None)
            for j, parent in enumerate(parents if i < 11 else parents[:1]):
                edge = deepcopy(edge_template)
                edge.update(id=f'{unit}_r{i}_{j}', type=relation, source=parent, target=area)
                graph['edges'].append(edge)

    policy = _policy(inputs)
    policy['reviewed_key_pairs'][0]['observation_digests'] = [
        reviewed_observation_digest('text', code, names[code]) for code in real_codes]
    before = merge_construction_graphs(inputs)
    after = merge_construction_graphs(inputs, merge_policy=policy)
    assert len(before['nodes']) - len(after['nodes']) == 50
    city = [node['identity']['code'] for node in after['nodes'] if node.get('original_type', node['type']) == 'District']
    assert sorted(city) == sorted([*real_codes, 'SNULL', 'SNULL'])
    assert sum(node['type'] == 'OtherAxis' for node in after['nodes']) == 50
    original_edges = {edge['id'] for c in inputs for edge in c['graph']['edges']}
    recovered = {edge['id'] for edge in after['edges']}
    recovered.update(origin['edge_id'] for edge in after['edges'] for support in edge['supports']
                     for origin in support.get('endpoint_origins', ()))
    assert len(original_edges) == len(after['edges']) == 113
    assert original_edges <= recovered
    assert after == merge_construction_graphs(inputs[::-1], merge_policy=policy)
