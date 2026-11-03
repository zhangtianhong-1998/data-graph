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


def test_invalid_json_retries_same_request_and_keeps_validated_draft_obligations(tmp_path, monkeypatch):
    source_table = table('items', ['tenant', 'key', 'label', 'unit', 'amount'])
    rows = {'public.items': [('North', 'A', 'Alpha', '元', '901'),
                             ('South', 'A', 'Alpha', '元', '902')]}
    requests = []

    def handler(request):
        body, packet = input_packet(request)
        assert packet['task'] in ('T0_discovery', 'T1_table_discovery')
        requests.append(body)
        if len(requests) == 1:
            return tools_response(('submit_column_group', retained_group(packet, namespace='tenant', extra=[
                ('unit', {'role': 'semantic_attribute', 'property_name': 'unit'}),
                ('amount', {'role': 'fact_value', 'exclude_from_projection': True})])))
        if len(requests) == 2:
            assert any(message['role'] == 'tool' and 'draft_1' in message['content']
                       for message in body['messages'])
            return broken_json()
        assert len(requests) == 3
        assert body['tools'] == requests[1]['tools']
        # Retry retains the acknowledged tool result, not the unusable reply.
        before = [m for m in requests[1]['messages'] if m['role'] == 'tool']
        assert [m for m in body['messages'] if m['role'] == 'tool'] == before
        assert PRIVATE not in json.dumps(body)
        return response({'action': 'finish_discovery', 'pending': False})

    model = client(monkeypatch, handler)
    with MemorySource([source_table], rows) as source:
        report = run_pipeline(RunConfig(sources=[], output_dir=str(tmp_path),
                              scopes=['public', 'North', 'South'], max_identity_reviews=0),
                              [source], model=model)
    assert report.status == 'COMPLETED', report.issues
    assert len(requests) == model.usage['calls'] == 3
    assert model.usage['failures'] == 1
    assert model.usage['request_retry_attempts'] == model.usage['request_retry_succeeded'] == 1
    assert not report.counts['work_deferred']
    assert model.budget.snapshot()['reserved'] == {}
    with GraphStore(tmp_path/'graph.sqlite') as store:
        rules = store.get_rules()
        assert len(rules) == 1
        rule = rules[0]
        assert rule.identity.key_columns == ['tenant', 'key'] and rule.scope.namespaces == ['tenant']
        assert rule.attributes.single_valued['unit'] == 'unit'
        assert any(item.column == 'amount' for item in rule.attributes.excluded)
        assert rule.evidence.semantic_basis  # Original issued column evidence survived.
        nodes = [n for n in store.view({'public', 'North', 'South'})['nodes'] if n['layer'] == 'business']
        assert len(nodes) == len({n['id'] for n in nodes}) == 2
        assert all(n['label'] == 'Alpha' and n['properties']['unit'] == '元' for n in nodes)
    assert retain_check([p for p in tmp_path.iterdir() if p.name!='traces'], [PRIVATE])['violations'] == 0
    assert any(PRIVATE in p.read_text(encoding='utf-8') for p in (tmp_path/'traces').glob('*.trace.jsonl'))


def test_exhausted_request_retry_defers_only_affected_task_and_preserves_other_rules(tmp_path, monkeypatch):
    tables = [table(name, ['key', 'label']) for name in ('a_healthy', 'b_affected')]
    rows = {'public.a_healthy': [('A', 'Healthy')], 'public.b_affected': [('B', 'Affected')]}
    attempts = Counter()

    def handler(request):
        _, packet = input_packet(request)
        name = packet['scope']['table']
        if packet['task'] == 'T2_cross_table':
            return response({'action': 'finish_discovery', 'pending': False})
        attempts[name] += 1
        if name == 'a_healthy':
            return response(group(packet, 'key', 'label'))
        if attempts[name] == 1:
            return tools_response(('submit_column_group', retained_group(packet)))
        return broken_json()

    model = client(monkeypatch, handler)
    with MemorySource(tables, rows) as source:
        report = run_pipeline(RunConfig(sources=[], output_dir=str(tmp_path), max_identity_reviews=0),
                              [source], model=model)
    assert report.status == 'PARTIAL', report.issues
    assert attempts == {'a_healthy': 1, 'b_affected': 3}
    assert model.usage['request_retry_attempts'] == model.usage['request_retry_exhausted'] == 1
    assert any(i.get('table') == 'public.b_affected' and i.get('status') == 'deferred'
               for i in report.issues)
    assert not any(i.get('table') == 'public.a_healthy' and i.get('status') in ('deferred', 'partial')
                   for i in report.issues)
    with GraphStore(tmp_path/'graph.sqlite') as store:
        nodes = [n for n in store.view({'public'})['nodes'] if n['layer'] == 'business']
        assert 'Healthy' in {n['label'] for n in nodes}
        assert not report.counts['publication_groups_failed']
        work = store.connection.execute(
            "SELECT source_ref,state FROM iteration_work WHERE state!='superseded'").fetchall()
        deferred_tables = {json.loads(row['source_ref'])['table'] for row in work if row['state'] == 'deferred'}
        assert deferred_tables == {'b_affected'}
    assert retain_check([p for p in tmp_path.iterdir() if p.name!='traces'], [PRIVATE])['violations'] == 0
    assert any(PRIVATE in p.read_text(encoding='utf-8') for p in (tmp_path/'traces').glob('*.trace.jsonl'))

    recovered_metadata = []

    def recovered_handler(request):
        _, packet = input_packet(request)
        if packet['task'] == 'T2_cross_table':
            return response({'action': 'finish_discovery', 'pending': False})
        name = packet['scope']['table']
        recovered_metadata.append(name)
        assert name == 'b_affected'  # A published draft does not erase unfinished investigation.
        return response(group(packet, 'key', 'label'))

    with MemorySource(tables, rows) as source:
        recovered = run_pipeline(RunConfig(sources=[], output_dir=str(tmp_path), max_identity_reviews=0),
                                 [source], model=client(monkeypatch, recovered_handler))
    assert recovered_metadata == ['b_affected']
    assert recovered.status == 'COMPLETED', recovered.issues
    assert recovered.counts['work_deferred'] == 0
    assert recovered.counts['business_nodes'] == 2
    assert not any(i.get('status') == 'deferred' for i in recovered.issues)


def test_request_retry_cannot_exceed_global_model_call_budget(tmp_path, monkeypatch):
    source_table = table('items', ['key', 'label'])
    calls = []

    def handler(request):
        _, packet = input_packet(request)
        calls.append(1)
        if len(calls) == 1:
            return tools_response(('submit_column_group', retained_group(packet)))
        return broken_json()

    model = client(monkeypatch, handler)
    model.budget = BudgetAllocator(Cost(db_cu=160, llm_calls=2, llm_tokens=None,
        memory_bytes=8*1024*1024, seconds=600))
    with MemorySource([source_table], {'public.items': [('A', 'Alpha')]}) as source:
        report = run_pipeline(RunConfig(sources=[], output_dir=str(tmp_path), max_identity_reviews=0),
                              [source], model=model)
    assert report.status == 'PARTIAL', report.issues
    assert len(calls) == model.usage['calls'] == model.budget.snapshot()['used']['llm_calls'] == 2
    assert any(i['code'] == 'llm_calls_budget_exhausted' for i in report.issues)
    assert model.budget.snapshot()['reserved'] == {}


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
