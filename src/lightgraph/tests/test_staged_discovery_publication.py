"""Unfinished Agent submissions must publish valid work without closing discovery."""
import pytest

from lightgraph.pipeline import run_pipeline
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.tests.test_agent_pipeline import client, group, input_packet, response, table


@pytest.mark.parametrize('pending_code, invalid_count', [
    ('discovery_submission_incomplete', 0),
    ('discovery_submission_pending', 0),
    ('discovery_invalid_drafts', 1),
])
def test_valid_draft_publishes_and_unfinished_discovery_retries(monkeypatch, tmp_path,
                                                              pending_code, invalid_count):
    source_table = table('drafts', ['key', 'label'])
    tasks = []
    def handler(request):
        _, packet = input_packet(request)
        tasks.append(packet['task'])
        return response(group(packet, 'key', 'label'))
    model = client(monkeypatch, handler)
    original_explore = model.explore
    def unfinished(*args, **kwargs):
        batch = original_explore(*args, **kwargs)
        batch._runtime_pending_codes = [pending_code]
        batch._runtime_invalid_drafts = invalid_count
        return batch
    model.explore = unfinished
    config = RunConfig(sources=[], output_dir=str(tmp_path), max_identity_reviews=0)
    rows = {'public.drafts': [('K', 'Name')]}
    with MemorySource([source_table], rows) as source:
        first = run_pipeline(config, [source], model=model)
    assert first.counts['business_nodes'] == 1
    assert first.status == 'PARTIAL'
    assert first.validation['graph_export'] == 'written'
    assert any(issue['code'] == pending_code and issue['status'] == 'deferred'
               for issue in first.issues)
    assert first.counts['discovery_pending_submissions'] >= 1
    if invalid_count:
        assert first.counts['discovery_invalid_drafts'] >= invalid_count
    tasks.clear()
    with MemorySource([source_table], rows) as source:
        second = run_pipeline(config, [source], model=client(monkeypatch, handler))
    assert 'T0_discovery' in tasks
    assert second.counts['business_nodes'] == 1
    assert second.counts['work_deferred'] == 0
