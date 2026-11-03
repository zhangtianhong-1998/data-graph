"""Provider request failures are isolated from retained drafts and task status."""
from collections import Counter
import json
from types import SimpleNamespace

import httpx

from lightgraph.discovery.budget import BudgetAllocator, Cost
from lightgraph.discovery.models import ProposalBatch
from lightgraph.discovery.workflow import _record_submission_state
from lightgraph.pipeline import run_pipeline
from lightgraph.runtime.config import RunConfig
from lightgraph.runtime.retention import retain_check
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import client, group, input_packet, response, table
from lightgraph.tests.test_proposal_session import tools_response


PRIVATE = 'MALFORMED_PROVIDER_RESPONSE_7281'


def broken_json():
    return httpx.Response(200, json={'choices': [{'message': {
        'content': '{"action":"finish_discovery", "private":"' + PRIVATE},
        'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 30, 'completion_tokens': 20}})


def retained_group(packet, *, namespace=None, extra=()):
    proposal = group(packet, 'key', 'label', namespace=namespace, extra=extra)['proposals'][0]
    return {name: value for name, value in proposal.items() if name != 'kind'}








def test_submission_keeps_registered_failure_cause_without_accepting_arbitrary_codes():
    result = SimpleNamespace(issues=[], stats={})
    batch = ProposalBatch(task='T0_discovery', abstain=True)
    batch._runtime_pending_codes = ['discovery_submission_incomplete',
        'llm_schema_invalid', 'llm_calls_budget_exhausted',
        'model_transport_PRIVATE_PROVIDER_MESSAGE', PRIVATE]
    _record_submission_state(result, table('items', ['key']), batch)
    assert {issue['code'] for issue in result.issues} == {
        'discovery_submission_incomplete', 'llm_schema_invalid', 'llm_calls_budget_exhausted'}
    assert all(issue['status'] == 'deferred' for issue in result.issues)
    assert 'PRIVATE' not in json.dumps(result.issues)
