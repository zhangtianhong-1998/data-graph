"""Vectorization survives closing the page, deduplicates clicks and reports interruptions."""
from threading import Event

import pytest

from lightgraph.errors import LightGraphError
from lightgraph.web.retrieval_jobs import RetrievalJobs
from lightgraph.web.workspace import Workspace


def test_refresh_rejoins_build_and_an_interrupted_or_failed_build_can_be_retried(tmp_path):
    workspace = Workspace(tmp_path)
    workspace.create('demo', 'demo')
    path = workspace.project_path('demo') / 'vectorization-job.json'
    path.write_text('{"status":"running","job_id":"former"}', encoding='utf-8')
    jobs = RetrievalJobs(workspace)
    release = Event()
    entered = Event()

    def build():
        entered.set()
        assert release.wait(5)
        raise RuntimeError('private driver details')

    try:
        assert jobs.status('demo')['status'] == 'interrupted'
        first, future = jobs.start('demo', 4, build)
        assert entered.wait(5)
        again, duplicate = jobs.start('demo', 4, lambda: pytest.fail('duplicate build'))
        assert jobs.status('demo') == again == first
        assert duplicate is future
        with pytest.raises(LightGraphError, match='retrieval_job_active'):
            jobs.start('demo', 5, build)
        release.set()
        with pytest.raises(LightGraphError, match='retrieval_build_failed'):
            future.result(timeout=5)
        assert jobs.status('demo')['status'] == 'failed'
        assert 'private driver details' not in path.read_text(encoding='utf-8')
        second, retry = jobs.start('demo', 5, lambda: {'vector_cards': 3})
        assert retry.result(timeout=5) == {'vector_cards': 3}
        assert jobs.status('demo')['status'] == 'completed' and second['job_id'] != first['job_id']
    finally:
        release.set()
        jobs.shutdown()


def test_sync_reports_real_batches_before_publication(tmp_path):
    from lightgraph.retrieval import sync_graph_to_retrieval
    from lightgraph.tests.test_retrieval_fields import FixtureEmbedder
    from lightgraph.tests.test_retrieval_fields import _scoped_graph

    embedder = FixtureEmbedder()
    embedder.batch_size = 2
    seen = []

    def progress(stage, completed, total):
        assert not (tmp_path / 'current.json').exists()
        seen.append((stage, completed, total))

    result = sync_graph_to_retrieval(_scoped_graph(), tmp_path, embedder=embedder, progress=progress)
    stage, total, _ = seen[-1]
    assert stage == 'publishing' and 2 < total <= result['vector_rows']
    assert seen[:-1] == [('embedding', done, total) for done in [*range(0, total, 2), total]]
