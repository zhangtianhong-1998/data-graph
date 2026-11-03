"""Rejected input is diagnostic; unfinished investigations remain resumable."""
from collections import Counter

import pytest

from lightgraph.pipeline import run_pipeline
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.tests.test_agent_pipeline import client, group, input_packet, response, table
from lightgraph.tests.test_proposal_session import tools_response


@pytest.mark.parametrize('pending,status', [(False, 'COMPLETED'), (True, 'PARTIAL')])
def test_bad_reference_attempt_then_finish_preserves_graph_and_actual_work(
        tmp_path, monkeypatch, pending, status):
    tables = [table('items', ['key', 'label']), table('dimensions', ['key', 'label'])]
    rows = {'public.items': [('A', 'Alpha')], 'public.dimensions': [('D', 'Dimension')]}
    attempts = Counter()

    def handler(request):
        _, packet = input_packet(request)
        name = packet['scope']['table']
        if packet['task'] in ('T0_discovery', 'T1_table_discovery'):
            kind = 'Dimension' if name == 'dimensions' else 'ReportItem'
            return response(group(packet, 'key', 'label', kind))
        assert packet['task'] == 'T2_cross_table'
        attempts[name] += 1
        if attempts[name] == 1:
            return tools_response(('submit_reference', {'claim': {
                'source_columns': ['unissued_column'], 'target_columns': ['unissued_column']}}))
        return response({'action': 'finish_discovery', 'pending': pending})

    config = RunConfig(sources=[], output_dir=str(tmp_path), max_identity_reviews=0)
    with MemorySource(tables, rows) as source:
        report = run_pipeline(config, [source], model=client(monkeypatch, handler))
    assert report.status == status, report.issues
    assert len(report.rules_published) == 2
    assert report.counts['business_nodes'] == 2 and report.counts['business_edges'] == 0
    assert report.counts['discovery_invalid_drafts'] == 2
    assert bool(report.counts['work_deferred']) is pending
    assert all(i['status'] == 'diagnostic' for i in report.issues
               if i['code'] in {'discovery_draft_rejected', 'discovery_draft_field_diagnostic'})
    assert any(i['code'] == 'discovery_submission_pending' for i in report.issues) is pending
    if not pending:
        def unexpected_call(request):
            raise AssertionError('Completed investigation must not be repeated')
        with MemorySource(tables, rows) as source:
            resumed = run_pipeline(config, [source], model=client(monkeypatch, unexpected_call))
        assert resumed.status == 'COMPLETED' and resumed.counts['llm_calls'] == 0
        assert resumed.counts['business_nodes'] == 2
