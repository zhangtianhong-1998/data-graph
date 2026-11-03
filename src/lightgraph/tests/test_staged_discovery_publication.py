"""Published drafts survive interrupted discovery, which resumes until finished."""
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
def test_valid_draft_is_revalidated_while_unfinished_discovery_resumes(monkeypatch, tmp_path,
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
    updated_rows={'public.drafts':[('K','Name'),('K2','New object')]}
    with MemorySource([source_table], updated_rows) as source:
        second = run_pipeline(config, [source], model=client(monkeypatch, handler))
    assert tasks.count('T0_discovery') == 1
    assert second.status == 'COMPLETED'
    assert set(second.rules_published) == set(first.rules_published)
    assert second.counts['business_nodes'] == 2
    assert second.counts['work_deferred'] == 0
    tasks.clear()
    with MemorySource([source_table], updated_rows) as source:
        third = run_pipeline(config, [source], model=client(monkeypatch, handler))
    assert not tasks and third.status == 'COMPLETED'
    assert third.counts['reused_rules'] == 1
    assert third.counts['business_nodes'] == 2
