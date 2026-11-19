"""Pin authorization, version and scope for each persisted offline projection."""
import time

from fastapi.testclient import TestClient

from lightgraph.tests.test_web_graph_history import publish
from lightgraph.web.app import create_app
from lightgraph.web.workspace import Workspace


def test_published_version_computes_once_and_history_retains_its_own_projection(tmp_path):
    workspace = Workspace(tmp_path / 'workspace')
    workspace.create('Analysis', 'p')
    config = workspace.result_config('p')
    first = publish(config, 'first')
    url = '/api/projects/p/graph/analytics'
    with TestClient(create_app(workspace.root)) as client:
        jobs = client.app.state.analytics_jobs
        assert client.get(url).json()['status'] == 'not_built'
        jobs.published('p', config)
        for _ in range(200):
            result = client.get(url).json()
            if result['status'] not in {'running', 'queued'}:
                break
            time.sleep(.01)
        assert result['status'] == 'completed', result
        assert result['counts']['nodes'] == 1
        assert [item['id'] for item in result['nodes']] == ['first']
        assert client.post(url).json()['status'] == 'completed'
        second = publish(config, 'second')
        assert second != first
        assert client.get(url).json()['status'] == 'not_built'
        assert client.get(url, params={'version': first}).json()['version_id'] == first
        # Context changes have their own topology and result, including an empty view.
        params = {'version': first, 'business_context': '{"project":"K"}'}
        assert client.get(url, params=params).json()['status'] == 'not_built'
        assert client.post(url, params=params).status_code == 202
        for _ in range(200):
            filtered = client.get(url, params=params).json()
            if filtered['status'] == 'completed':
                break
            time.sleep(.01)
        assert filtered['nodes'] == [] and filtered['counts']['nodes'] == 0



def test_question_only_reads_versioned_overview_and_requires_correct_projection(tmp_path):
    workspace = Workspace(tmp_path / 'workspace')
    workspace.create('Analysis', 'p')
    raw = workspace.get_config('p')
    raw['embedding'] = {'provider': 'local_hash'}
    workspace.save_config('p', raw)
    version = publish(workspace.result_config('p'), 'visible')
    with TestClient(create_app(workspace.root)) as client:
        base = '/api/projects/p'
        assert client.post(base + '/retrieval/sync').status_code == 200
        body = {'query': '全图概览，有哪些社区和关键节点？'}
        def overview():
            response = client.post(base + '/retrieval/query', json=body)
            assert response.status_code == 200, response.text
            return response.json()
        assert overview()['overview']['status'] == 'analysis_not_ready'
        assert client.post(base + '/graph/analytics').status_code == 202
        for _ in range(200):
            if client.get(base + '/graph/analytics').json()['status'] == 'completed':
                break
            time.sleep(.01)
        result = overview()
        assert result['version_id'] == version
        assert result['plan']['intent'] == 'overview'
        assert [n['id'] for n in result['nodes']] == ['visible']
        body['query'] = '哪些节点的入度最高？'
        ranking = overview()
        assert ranking['overview']['ranking_metric'] == 'in_degree'
        assert ranking['nodes'][0]['in_degree'] == 0
        # A restricted projection cannot inherit the unrestricted community membership.
        body['business_context'] = {'project':'M'}
        assert overview()['overview']['status'] == 'analysis_not_ready'


def test_failed_analysis_keeps_published_graph_and_explicit_retry_succeeds(tmp_path, monkeypatch):
    from lightgraph.web import analytics_jobs
    workspace = Workspace(tmp_path / 'workspace')
    workspace.create('Analysis', 'p')
    version = publish(workspace.result_config('p'), 'ready')
    compute = analytics_jobs.persist_analysis
    def fail(*args):
        raise ValueError('internal details are not persisted')
    monkeypatch.setattr(analytics_jobs, 'persist_analysis', fail)
    with TestClient(create_app(workspace.root)) as client:
        api = '/api/projects/p/graph'
        assert client.post(api + '/analytics').status_code == 202
        for _ in range(200):
            failed = client.get(api + '/analytics').json()
            if failed['status'] == 'failed':
                break
            time.sleep(.01)
        assert failed['error_code'] == 'graph_analytics_failed'
        assert client.get(api).json()['version_id'] == version
        monkeypatch.setattr(analytics_jobs, 'persist_analysis', compute)
        assert client.post(api + '/analytics', params={'force': 'true'}).status_code == 202
        for _ in range(200):
            retried = client.get(api + '/analytics').json()
            if retried['status'] == 'completed':
                break
            time.sleep(.01)
        assert retried['counts']['nodes'] == 1


def test_interrupted_job_is_recovered_for_its_original_version(tmp_path):
    import json
    from lightgraph.construction.analytics_store import artifact_path
    workspace = Workspace(tmp_path / 'workspace')
    workspace.create('Analysis', 'p')
    config = workspace.result_config('p')
    old = publish(config, 'before')
    from pathlib import Path
    path = artifact_path(Path(config.output_dir) / 'construction', config, old, None)
    directory = workspace.project_path('p') / 'analytics-jobs'
    directory.mkdir()
    (directory / path.name).write_text(json.dumps({'version_id': old, 'status': 'running', 'business_context': None}))
    latest = publish(config, 'after')
    with TestClient(create_app(workspace.root)) as client:
        api = '/api/projects/p/graph/analytics'
        for _ in range(200):
            recovered = client.get(api, params={'version': old}).json()
            if recovered['status'] == 'completed':
                break
            time.sleep(.01)
        assert recovered['nodes'][0]['id'] == 'before'
        assert client.get(api).json() == {'version_id': latest, 'status': 'not_built'}
