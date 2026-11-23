from copy import deepcopy

from lightgraph.construction.checkpoints import commit_checkpoint, load_checkpoint
from lightgraph.construction.access import access_fingerprint
from lightgraph.construction.graph_storage import read_graph_record
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.construction.workflow import deterministic_stage_inputs, effective_merge_policy, resolve_workflow_plan
from lightgraph.construction.workspace import read_internal_yaml
from lightgraph.runtime.config import RunConfig
from lightgraph.tests.construction_worker_fixture import hierarchy_proposal, install_scripted_worker
from lightgraph.tests.test_observed_identity import projection, unit
from lightgraph.tests.test_observed_dictionary_merge import _units
from lightgraph.tests.test_source_hierarchy_replays import contributor, draft_graph
from lightgraph.tests.test_web_api import client, create, upload
from lightgraph.tests.test_web_execution import wait_run


def test_http_workers_publish_same_hierarchy_as_one_merge_of_all_committed_leaves(client, monkeypatch):
    install_scripted_worker(monkeypatch, draft_factory=hierarchy_proposal)
    create(client)
    upload(client, 'catalog.csv', b'parent_code,parent_name,child_code,child_name\nP,Root,A,Member\nP,Root,B,Member\n')
    api = '/api/projects/alpha'
    cfg = client.get(api + '/config').json()
    cfg['goal']['purpose'] = '合并三个目录对同一层级的观察'
    cfg['model'].update(mode='in_environment', model='scripted-test', base_url='http://127.0.0.1:9999/v1')
    source = cfg['sources'][0]['id']
    units = {'a': 'Entry', 'b': 'Term', 'c': 'Subject'}
    # The terminal consumes b a second time, along both the direct edge and the intermediate merge.
    parents = {'ab': ['a', 'b'], 'terminal': ['ab', 'b', 'c']}
    cfg['workflow'] = {'enabled': True, 'max_parallel_workers': 2,
        'units': [{'unit_id': uid, 'source_ids': [source], 'goal_markdown': kind} for uid, kind in units.items()],
        'merge_nodes': [{'node_id': name} for name in parents],
        'edges': [{'edge_id': parent + '_' + name, 'source': parent, 'target': name}
                  for name, incoming in parents.items() for parent in incoming]}
    saved = client.put(api + '/config', json=cfg)
    assert saved.is_success, saved.text
    started = client.post(api + '/runs')
    assert started.status_code == 202, started.text
    run = wait_run(client.app.state.manager, 'alpha', started.json()['run_id'], timeout=60)
    state = run['workflow_state']
    assert run['status'] == 'completed', state
    workspace = client.app.state.workspace
    config = workspace.resolved_config('alpha')
    output = workspace.project_path('alpha') / run['output_path']
    artifacts = state['artifacts']
    leaves = [load_checkpoint(output, artifacts[name]) for name in units]
    expected = merge_construction_graphs(leaves, merge_policy=effective_merge_policy(config),
                                         target_access_fingerprint=access_fingerprint(config))
    for name, incoming in parents.items():
        assert set(artifacts[name]['upstream_artifacts']) == {artifacts[p]['artifact_id'] for p in incoming}
    intermediate = load_checkpoint(output, artifacts['ab'])['graph']
    assert (len(intermediate['nodes']), len(intermediate['edges'])) == (3, 4)
    final = load_checkpoint(output, artifacts['terminal'])['graph']
    assert (len(final['nodes']), len(final['edges'])) == (3, 6)
    assert final['fusion_diagnostics']['input_nodes'] == 9
    assert final == expected
    construction = output / 'construction'
    root_graph = read_internal_yaml(construction / 'manifest.yaml')['current_graph']['path']
    assert read_graph_record(construction / root_graph) == {**expected, 'storage_layout': 'parquet_v1'}


def test_checkpoint_dag_rechecks_all_originals_and_deduplicates_diamond_inputs(tmp_path):
    units = {name: contributor(name, draft_graph(i, kind)) for i, (name, kind) in enumerate(
        [('one', 'Entry'), ('two', 'Term'), ('three', 'Subject')], start=1)}
    parents = {'ab': ['one', 'two'], 'bc': ['two', 'three'], 'root': ['ab', 'bc']}
    config = RunConfig(sources=[{'id': 'file', 'kind': 'excel_workbook', 'path': 'unused.xlsx'}],
        workflow={'enabled': True, 'units': [{'unit_id': name, 'source_ids': ['file']} for name in units],
            'merge_nodes': [{'node_id': name} for name in parents],
            'edges': [{'edge_id': name + parent, 'source': parent, 'target': name}
                      for name, incoming in parents.items() for parent in incoming]})
    run_id = 'run_' + '1' * 24
    plan = resolve_workflow_plan(config.workflow)
    nodes = {n.node_id: n for n in config.workflow.merge_nodes}
    policy = effective_merge_policy(config)
    artifacts = {name: commit_checkpoint(tmp_path, run_id, name, u, input_digest=name, graph_kind='node_delta')
                 for name, u in units.items()}
    units = {name: load_checkpoint(tmp_path, record) for name, record in artifacts.items()}
    merged = {}
    for name in ['ab', 'bc']:
        inputs = [units[parent] for parent in parents[name]]
        stage = {'contributor_id': name, 'kind': 'merged', 'graph': merge_construction_graphs(inputs, merge_policy=policy)}
        artifacts[name] = commit_checkpoint(tmp_path, run_id, name, stage, input_digest=name,
            upstream_artifacts=[artifacts[parent]['artifact_id'] for parent in parents[name]])
        merged[name] = load_checkpoint(tmp_path, artifacts[name])
    def inputs():
        return deterministic_stage_inputs(parents['root'], units=units, merged=merged, merge_nodes=nodes,
            plan=plan, artifacts=artifacts, policy=policy, config=config, bases=[])
    assert {c['contributor_id'] for c in inputs()} == set(units)
    staged = merge_construction_graphs(inputs(), merge_policy=policy)
    assert staged == merge_construction_graphs(list(units.values()), merge_policy=policy)
    assert len(staged['nodes']) == 3 and len(staged['edges']) == 6
    # Independent worker deltas and incomplete publications retain their boundary.
    nodes['ab'].merge_mode = 'merge_and_extract'
    assert inputs() == [merged['ab'], merged['bc']]
    nodes['ab'].merge_mode = 'deterministic'
    artifacts['bc']['coverage'] = 'incomplete'
    assert inputs() == [merged['ab'], merged['bc']]
    artifacts['bc']['coverage'] = 'complete'
    artifacts['two']['coverage'] = 'incomplete'
    assert inputs() == [merged['ab'], merged['bc']]


def test_same_unique_row_with_different_language_columns_produces_reviewable_candidate():
    first = _units()[0]
    second = deepcopy(first)
    second['contributor_id'] = 'translation'
    definition, = second['graph']['definitions']['entities']
    definition['type'] = 'TranslatedMember'
    for node in second['graph']['nodes']:
        node.update(type=definition['type'], label='Revenue')
        for support in node['supports']:
            support['label'] = 'Revenue'
            support['provenance']['label_column'] = 'english_title'
    graph = merge_construction_graphs([first, second])
    assert len(graph['nodes']) == 2
    candidate, = graph['candidate_identity_merges']
    assert candidate['status'] == 'candidate_only'
    assert set(candidate['node_ids']) == {n['id'] for n in graph['nodes']}
    frozen = merge_construction_graphs([{'kind': 'merged', 'contributor_id': 'inner', 'graph': graph}])
    assert frozen['candidate_identity_merges'] == graph['candidate_identity_merges']


def test_different_meanings_of_same_relation_keep_both_edges_and_descriptions():
    left, right = projection(), projection()
    for graph, meaning in [(left, '行政汇报'), (right, '预算分摊')]:
        graph['definitions']['relations'][0]['description'] = meaning
    contributors = [unit('reports', left), unit('budget', right)]
    merged = merge_construction_graphs(contributors)
    assert len(merged['edges']) == 2
    assert {r['description'] for r in merged['definitions']['relations']} == {'行政汇报', '预算分摊'}
    assert len({edge['type'] for edge in merged['edges']}) == 2
    assert all(s['endpoint_origins'][0]['type'] == 'contains' for e in merged['edges'] for s in e['supports'])
    assert merged['definition_conflicts']
    assert merge_construction_graphs(contributors[::-1]) == merged
