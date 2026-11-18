"""Pinned graph pages, portable result permissions and migration via the public API."""
from pathlib import Path, PureWindowsPath
import shutil

from fastapi.testclient import TestClient

from lightgraph.construction.merger import write_sharded_graph
from lightgraph.construction.snapshot_registry import register_access_context, snapshot_id
from lightgraph.construction.workspace import read_internal_yaml
from lightgraph.tests.test_graph_storage_integration import graph
from lightgraph.web.app import create_app
from lightgraph.web.workspace import Workspace


def publish(config, label, *, portable=True):
    root = Path(config.output_dir) / 'construction'
    policy = register_access_context(root, config) if portable else None
    value = graph(config, label, label)
    secret = graph(config, 'private-node', 'PRIVATE')['nodes'][0]
    secret['supports'][0]['scope_ref'] = 'secret'
    value['nodes'].append(secret)
    write_sharded_graph(root, value, context_fingerprint=label, result_policy_id=policy)
    return snapshot_id(read_internal_yaml(root / 'manifest.yaml')['current_graph'], root=root)


def test_pinned_canvas_details_batches_survive_new_head_and_offline_project_move(tmp_path):
    original = Workspace(tmp_path / 'original')
    original.create('History', 'p')
    file = original.add_file('p', 'source.csv', b'id\n1\n')
    cfg = original.resolved_config('p')
    version = publish(cfg, 'first')
    (original.project_path('p') / file['relative_path']).unlink()
    moved = tmp_path / 'moved'
    shutil.move(original.root, moved)
    workspace = Workspace(moved)
    base = '/api/projects/p/graph'
    with TestClient(create_app(moved, frontend_dir=tmp_path / 'absent')) as client:
        assert client.get('/api/projects/p/config').status_code == 200
        first = client.get(base + '/batches').json()
        assert first['version_id'] == version and first['total_nodes'] == 1
        cfg = workspace.result_config('p')
        other = publish(cfg, 'second')
        assert other != version
        listing = client.get(base + '/versions').json()
        assert listing['current_version_id'] == other and len(listing['versions']) == 2
        assert all(v['readable'] and 'nodes' not in v for v in listing['versions'])
        assert client.get(base).json()['nodes'][0]['id'] == 'second'
        page = client.get(base, params={'version': version}).json()
        assert page['version_id'] == version and [n['id'] for n in page['nodes']] == ['first']
        assert 'PRIVATE' not in str(page)
        selection = {'version': version, 'generation': first['generation'], 'part': 'edges'}
        assert client.get(base + '/batches', params=selection).status_code == 200
        details = client.get(base + '/details', params={'version': version, 'entity_id': 'first'}).json()
        assert details['version_id'] == version and details['supports']
        assert client.get(base + '/details', params={'version': version, 'entity_id': 'second'}).status_code == 404
        assert client.get(base, params={'version': '../output'}).status_code == 404
        assert workspace.output_generation('p') is None
        # A support-scope reduction applies to an already cached history view.
        cfg = workspace.get_config('p', offline=True)
        cfg['scopes'] = []
        workspace._write_config('p', cfg)
        assert client.get(base, params={'version': version}).json()['total_nodes'] == 0
        assert client.get(base + '/batches', params=selection).json()['error']['code'] == 'graph_generation_changed'


def test_windows_legacy_migration_requires_original_hash_and_current_grants(tmp_path):
    workspace = Workspace(tmp_path / 'workspace')
    workspace.create('History', 'p')
    workspace.add_file('p', 'source.csv', b'id\n1\n')
    current = workspace.result_config('p')
    historical = current.model_copy(deep=True)
    previous = PureWindowsPath('D:/experiment/p')
    relative = Path(historical.sources[0].path).relative_to(workspace.project_path('p'))
    historical.sources[0].path = str(previous.joinpath(*relative.parts))
    version = publish(historical, 'legacy', portable=False)
    base = '/api/projects/p/graph'
    with TestClient(create_app(workspace.root, frontend_dir=tmp_path / 'absent')) as client:
        entry, = client.get(base + '/versions').json()['versions']
        assert not entry['readable'] and entry['error_code'] == 'graph_access_context_changed'
        assert client.get(base, params={'version': version}).json()['error']['code'] == entry['error_code']
        request = base + '/versions/' + version + '/migrate'
        config = client.get('/api/projects/p/config')
        headers = {'If-Match': config.headers['etag']}
        assert client.post(request, json={'previous_project_root': str(previous)}).status_code == 428
        wrong = client.post(request, json={'previous_project_root': 'D:/wrong'}, headers=headers)
        assert wrong.json()['error']['code'] == 'graph_access_migration_evidence_mismatch'
        assert client.post(request, json={'previous_project_root': str(previous)}, headers=headers).status_code == 200
        assert client.get(base, params={'version': version}).json()['nodes'][0]['id'] == 'legacy'
        # Grants can still be withdrawn after a migration and a successful read.
        restricted = config.json()
        restricted['sources'][0]['allowed_columns'] = {'public.source': ['id']}
        assert client.put('/api/projects/p/config', json=restricted, headers=headers).status_code == 200
        assert client.get(base, params={'version': version}).json()['error']['code'] == 'graph_access_context_changed'


def test_checkpoint_and_worker_appear_without_project_publication(tmp_path):
    from lightgraph.construction.checkpoints import commit_checkpoint
    workspace = Workspace(tmp_path / 'workspace')
    workspace.create('Workflow', 'p')
    config = workspace.result_config('p')
    output = Path(config.output_dir)
    rid = 'run_' + 'a' * 24
    policy = register_access_context(output / 'construction', config)
    contributor = {'kind': 'merge', 'graph': graph(config, 'base'), 'contributor_id': 'merge_1'}
    commit_checkpoint(output, rid, 'merge_1', contributor, graph_kind='node_base',
                      input_digest='inputs', result_policy_id=policy)
    commit_checkpoint(output, rid, 'merge_1', {**contributor, 'graph': graph(config, 'merged')},
                      input_digest='inputs', result_policy_id=policy)
    worker = config.model_copy(update={'output_dir': str(output / 'attempts' / rid / 'extract_1')})
    publish(worker, 'unit')
    with TestClient(create_app(workspace.root, frontend_dir=tmp_path / 'absent')) as client:
        url = '/api/projects/p/graph'
        listing = client.get(url + '/versions').json()
        assert listing['current_version_id'] is None
        rows = listing['versions']
        assert len(rows) == 3 and all(r['run_id'] == rid and r['readable'] for r in rows)
        for row in rows:
            page = client.get(url, params={'version': row['version_id']}).json()
            expected = 'unit' if row['node_id'] == 'extract_1' else 'base' if row['graph_kind'] == 'node_base' else 'merged'
            assert page['nodes'][0]['id'] == expected
        assert not client.get(url).json()['nodes']


def test_merge_worker_rechecks_grants_on_its_upstream_project(tmp_path):
    """The worker reads source A, but its frozen upstream can also contain B."""
    from lightgraph.construction.checkpoints import commit_checkpoint
    from lightgraph.construction.workflow import build_merge_node_config
    ws = Workspace(tmp_path / 'workspace')
    ws.create('Workflow', 'p')
    for name in ('a', 'b'):
        ws.add_file('p', name + '.csv', b'id\n1\n')
    stored = ws.get_config('p')
    stored['workflow'] = {'enabled': True, 'units': [{'unit_id': 'a'}], 'merge_nodes': [
        {'node_id': 'merge', 'merge_mode': 'merge_and_extract', 'source_ids': [stored['sources'][0]['id']]}]}
    ws.save_config('p', stored)
    config = ws.result_config('p')
    output = Path(config.output_dir)
    rid = 'run_' + 'b' * 24
    base = commit_checkpoint(output, rid, 'merge', {'graph': graph(config, 'base')}, graph_kind='node_base',
                             input_digest='base', result_policy_id=register_access_context(output / 'construction', config))
    worker = build_merge_node_config(config, config.workflow.merge_nodes[0], upstream_summary='',
                                     node_output_dir=output / 'attempts' / rid / 'merge',
                                     upstream_snapshot_path=output / base['relative_path'])
    version = publish(worker, 'derived')
    with TestClient(create_app(ws.root, frontend_dir=tmp_path / 'absent')) as client:
        url = '/api/projects/p/graph'
        assert client.get(url, params={'version': version}).json()['nodes'][0]['id'] == 'derived'
        stored['sources'][1]['allowed_tables'] = ['only']
        ws.save_config('p', stored)
        assert client.get(url, params={'version': version}).json()['error']['code'] == 'graph_access_context_changed'


def test_worker_publications_pin_their_inherited_snapshots(tmp_path):
    from lightgraph.construction.inheritance import freeze_inherited_graphs
    from lightgraph.construction.snapshot_registry import record_graph_head
    from lightgraph.construction.workspace import GraphWorkspace, write_yaml
    from lightgraph.runtime.config import RunConfig
    from lightgraph.web.construction_graph import load_prepared
    output = tmp_path / 'output'
    source = tmp_path / 'base.yaml'
    config = RunConfig(sources=[], inherited_graphs=[{'inherit_id': 'base', 'snapshot_path': str(source)}],
                       output_dir=str(output))
    root = output / 'construction'
    ws = GraphWorkspace(root, 'ctx')
    policy = register_access_context(root, config)
    heads = []
    for label in ('first', 'second'):
        write_yaml(source, graph(config, label, label))
        freeze_inherited_graphs(config, root)
        head = {'context_fingerprint': 'ctx', 'path': ws._append_record('published', graph(config, 'delta', 'delta')),
                'access_fingerprint': graph(config)['access_fingerprint'], 'result_policy_id': policy}
        manifest = ws._manifest()
        record_graph_head(manifest, head, root=root)
        manifest['current_graph'] = head
        ws._save_manifest(manifest)
        heads.append(head)
    for head, wanted in zip(heads, ('first', 'second')):
        view, _ = load_prepared(config, root=root, head=head)
        assert {node['label'] for node in view['nodes']} == {wanted, 'delta'}


def test_copied_heads_in_two_generations_have_distinct_locators(tmp_path):
    ws = Workspace(tmp_path / 'workspace')
    ws.create('History', 'p')
    cfg = ws.result_config('p')
    original = publish(cfg, 'same')
    rid = 'run_' + 'c' * 24
    ws.select_output_generation('p', rid, create=True)
    shutil.copytree(Path(cfg.output_dir) / 'construction', ws.generation_path('p', rid) / 'construction')
    with TestClient(create_app(ws.root, frontend_dir=tmp_path / 'absent')) as client:
        url = '/api/projects/p/graph'
        listing = client.get(url + '/versions').json()
        copied = listing['current_version_id']
        assert copied != original and {r['version_id'] for r in listing['versions']} == {copied, original}
        assert client.get(url).json()['version_id'] == copied
        for version in (copied, original):
            page = client.get(url, params={'version': version}).json()
            assert page['version_id'] == version and page['nodes'][0]['id'] == 'same'


def test_external_project_revocation_applies_to_previously_cached_history(tmp_path):
    from lightgraph.construction.inheritance import freeze_inherited_graphs
    ws = Workspace(tmp_path / 'workspace')
    ws.create('Base', 'base')
    ws.create('Derived', 'derived')
    publish(ws.result_config('base'), 'source')
    cfg = ws.get_config('derived')
    cfg['inherited_graphs'] = [{'inherit_id': 'base', 'source_project_id': 'base', 'allowed_scopes': ['public']}]
    ws.save_config('derived', cfg)
    derived = ws.result_config('derived')
    freeze_inherited_graphs(derived, Path(derived.output_dir) / 'construction', workspace=ws)
    version = publish(derived, 'derived')
    with TestClient(create_app(ws.root, frontend_dir=tmp_path / 'absent')) as client:
        url = '/api/projects/derived/graph'
        first = client.get(url + '/batches', params={'version': version}).json()
        assert first['version_id'] == version
        changed = ws.get_config('base')
        changed['scopes'] = ['restricted']
        ws.save_config('base', changed)
        entry, = client.get(url + '/versions').json()['versions']
        assert entry['readable'] is False
        for endpoint, params in [(url, {}), (url, {'version': version}), (url + '/batches', {'version': version}),
                                 (url + '/details', {'version': version, 'entity_id': 'derived'})]:
            result = client.get(endpoint, params=params)
            assert result.json()['error']['code'] == 'inherited_graph_scope_not_authorized'


def test_transitive_source_revocation_blocks_browsing_and_workflow_reuse(tmp_path):
    import pytest
    from lightgraph.construction.inheritance import authorize_frozen_inheritance, freeze_inherited_graphs
    from lightgraph.errors import LightGraphError
    ws = Workspace(tmp_path / 'chain')
    ws.create('Source', 'source')
    publish(ws.result_config('source'), 'source')
    for target, parent in [('middle', 'source'), ('leaf', 'middle')]:
        ws.create(target, target)
        cfg = ws.get_config(target)
        cfg['inherited_graphs'] = [{'inherit_id': parent, 'source_project_id': parent, 'allowed_scopes': ['public']}]
        ws.save_config(target, cfg)
        resolved = ws.result_config(target)
        freeze_inherited_graphs(resolved, Path(resolved.output_dir) / 'construction', workspace=ws)
        version = publish(resolved, target)
    with TestClient(create_app(ws.root, frontend_dir=tmp_path / 'absent')) as client:
        url = '/api/projects/leaf/graph'
        assert client.get(url, params={'version': version}).status_code == 200
        changed = ws.get_config('source')
        changed['scopes'] = []
        ws.save_config('source', changed)
        assert client.get(url, params={'version': version}).json()['error']['code'] == 'inherited_graph_scope_not_authorized'
        with pytest.raises(LightGraphError, match='inherited_graph_scope_not_authorized'):
            authorize_frozen_inheritance(ws.result_config('leaf'), ws)


def test_republished_identical_blob_versions_its_run_permissions_and_frozen_inputs(tmp_path):
    from lightgraph.construction.snapshot_registry import publication_run_id, record_graph_head
    from lightgraph.construction.workspace import write_yaml

    ws = Workspace(tmp_path / 'workspace')
    ws.create('History', 'p')
    cfg = ws.result_config('p')
    root = Path(cfg.output_dir) / 'construction'
    publish(cfg, 'same')
    manifest_path = root / 'manifest.yaml'
    manifest = read_internal_yaml(manifest_path)
    published = manifest['current_graph']
    # Simulate a pre-history manifest. The first new publication must carry
    # the previous pointer verbatim, without assigning the new run to it.
    legacy = {k: published[k] for k in ('path', 'context_fingerprint', 'access_fingerprint', 'storage_layout')}
    manifest.update(current_graph=legacy)
    manifest.pop('graph_history')
    write_yaml(manifest_path, manifest)
    run_a, run_b = 'run_' + 'a' * 24, 'run_' + 'b' * 24
    token = publication_run_id.set(run_a)
    try:
        publish(cfg, 'same')
    finally:
        publication_run_id.reset(token)
    manifest = read_internal_yaml(manifest_path)
    assert legacy in manifest['graph_history']
    assert not {'run_id', 'created_at'} & set(legacy)

    # Keep the content path, access, context and timestamp fixed. Only one
    # field changes per publication below, including a new run sharing a blob.
    shared = {**legacy, 'created_at': '2026-11-17T00:00:00+00:00'}
    first = record_graph_head(manifest, {**shared, 'run_id': run_a}, root=root, graph_kind='project_full')
    second = record_graph_head(manifest, {**shared, 'run_id': run_b}, root=root, graph_kind='project_full')
    policy = register_access_context(root, cfg)
    third = record_graph_head(manifest, {**shared, 'run_id': run_b, 'result_policy_id': policy}, root=root, graph_kind='project_full')
    (root / 'inherited').mkdir(exist_ok=True)
    write_yaml(root / 'inherited/summary.yaml', {'format_version': 2, 'inherited': [], 'configuration_digest': 'another-base'})
    fourth = record_graph_head(manifest, {**shared, 'run_id': run_b, 'result_policy_id': policy}, root=root, graph_kind='project_full')
    manifest['current_graph'] = fourth
    write_yaml(manifest_path, manifest)
    versions = [head['version_id'] for head in (first, second, third, fourth)]
    assert len(set(versions)) == 4
    with TestClient(create_app(ws.root, frontend_dir=tmp_path / 'absent')) as client:
        url = '/api/projects/p/graph'
        listing = client.get(url + '/versions').json()
        assert listing['current_version_id'] == versions[-1]
        assert {r['version_id'] for r in listing['versions']}.issuperset(versions)
        for version in versions:
            page = client.get(url, params={'version': version}).json()
            assert page['version_id'] == version and page['nodes'][0]['id'] == 'same'
        old = next(r for r in listing['versions'] if r['version_id'] == snapshot_id(legacy, root=root))
        assert old['run_id'] is None and old['created_at'] is None
