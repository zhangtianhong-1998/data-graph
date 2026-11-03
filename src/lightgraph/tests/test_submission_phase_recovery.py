"""A stale exploration response must not consume the unused submission turn."""
import json

import httpx
import pytest

from lightgraph.discovery.budget import BudgetAllocator, Cost
from lightgraph.errors import BudgetExceeded, LightGraphError
from lightgraph.runtime.trace import RunTrace, bind_trace
from lightgraph.tests.test_agentscope_runtime import framework_spy, assert_cleared, reply_for
from lightgraph.tests.test_discovery_agent import Toolbox, input_packet, response
from lightgraph.tests.test_model_streaming import model
from lightgraph.tests.test_proposal_session import group, tools_response


BLOCKED_SQL = 'SELECT private_value FROM source_after_submission_started'


def runtime_feedback(body):
    for message in reversed(body['messages']):
        if message.get('role') != 'user':
            continue
        try:
            data = json.loads(message['content'])
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and 'runtime_feedback' in data:
            return data['runtime_feedback']
    pytest.fail('The corrective request must explain why no tools were executed')


@pytest.mark.parametrize('entrypoint', ['client', 'function'])
def test_default_window_can_explore_after_six_turns(entrypoint, framework_spy):
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        control = json.loads(body['messages'][1]['content'])['runtime_control']
        assert control['max_turns'] == 16
        assert not control['submission_required']
        if len(requests) <= 9:
            return response(tool='get_metadata', args={'probe_sequence': len(requests)})
        return tools_response(('submit_column_group', group()), ('finish_discovery', {}))

    client = model(handler, stream=False)
    tools = Toolbox()
    if entrypoint == 'function':
        from lightgraph.discovery.agent import explore
        batch = explore(client, input_packet(), tools)
    else:
        batch = client.explore(input_packet(), tools)
    assert len(batch.proposals) == 1 and batch._runtime_submission_complete
    assert tools.executed == ['get_metadata'] * 9
    assert len(requests) == client.usage['calls'] == 10
    assert_cleared(framework_spy)


@pytest.mark.parametrize('stream', [False, True])
def test_six_probes_then_stale_source_call_uses_last_turn_to_submit(stream, framework_spy, tmp_path):
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) <= 6:
            return response(tool='get_metadata', args={'probe_sequence': len(requests)})
        assert not ({'get_metadata', 'execute_readonly_sql', 'profile_columns'} & {
            item['function']['name'] for item in body['tools']})
        if len(requests) == 7:
            return response(tool='execute_readonly_sql', args={'sql': BLOCKED_SQL})
        assert len(requests) == 8
        assert body['tool_choice'] == 'required'
        feedback = runtime_feedback(body)
        assert feedback['code'] == 'source_tool_unavailable_during_submission'
        assert feedback['executed_tools'] == 0
        assert BLOCKED_SQL not in json.dumps(feedback)
        assert 'submit_column_group' in feedback['allowed_tools']
        return tools_response(('submit_column_group', group()), ('finish_discovery', {}))

    client = model(lambda request: reply_for(handler(request).json()['choices'][0]['message'], stream),
                   stream=stream)
    tools = Toolbox()
    path = tmp_path / 'submission.trace.jsonl'
    with bind_trace(RunTrace(path)):
        batch = client.explore(input_packet(), tools, max_steps=8, max_tool_calls=16)

    assert len(batch.proposals) == 1 and batch._runtime_submission_complete
    assert batch._runtime_pending_codes == []
    assert tools.executed == ['get_metadata'] * 6
    assert framework_spy['tool_names'] == ['get_metadata'] * 6 + ['submit_column_group', 'finish_discovery']
    assert client.usage['calls'] == 8 and client.usage['failures'] == 0
    records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    recorded_requests = [entry['data'] for entry in records if entry['kind'] == 'model_request']
    assert len(recorded_requests) == 8
    assert recorded_requests[-1]['discovery_step'] == 8
    assert recorded_requests[-1]['phase_correction'] is True
    assert runtime_feedback(recorded_requests[-1]['payload'])['executed_tools'] == 0
    assert client.usage['discovery_phase_rejections'] == 1
    assert client.usage['discovery_phase_recoveries'] == 1
    feedback_events = [entry['data'] for entry in records if entry['kind'] == 'discovery_phase_feedback']
    assert len(feedback_events) == 1 and feedback_events[0]['executed_tools'] == 0
    assert_cleared(framework_spy)


def test_mixed_blocked_batch_executes_no_submission_and_keeps_earlier_draft(framework_spy):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return response(tool='submit_column_group', args=group())
        if len(calls) == 2:
            return response(tool='get_metadata')
        if len(calls) == 3:
            second = group()
            second['claim']['group_id'] = 'must_not_execute'
            return tools_response(('submit_column_group', second),
                                  ('execute_readonly_sql', {'sql': BLOCKED_SQL}))
        assert runtime_feedback(body)['executed_tools'] == 0
        return response(tool='finish_discovery')

    client = model(handler, stream=False)
    tools = Toolbox()
    batch = client.explore(input_packet(), tools, max_steps=4, max_tool_calls=16)
    assert [proposal.claim.group_id for proposal in batch.proposals] == ['g']
    assert batch._runtime_submission_complete and batch._runtime_invalid_drafts == 0
    assert not batch._runtime_pending_codes
    assert tools.executed == ['get_metadata']
    assert framework_spy['tool_names'] == ['submit_column_group', 'get_metadata', 'finish_discovery']
    assert client.usage['calls'] == 4
    assert_cleared(framework_spy)


def test_repeated_stale_calls_stop_only_at_actual_step_limit(framework_spy):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return response(tool='get_metadata', args={'probe_sequence': len(requests)})

    client = model(handler, stream=False)
    tools = Toolbox()
    with pytest.raises(BudgetExceeded, match='discovery_steps_exhausted'):
        client.explore(input_packet(), tools, max_steps=8, max_tool_calls=16)
    assert len(requests) == client.usage['calls'] == 8
    assert tools.executed == ['get_metadata'] * 6
    assert runtime_feedback(requests[-1])['executed_tools'] == 0
    assert client.budget.snapshot()['reserved'] == {}
    assert_cleared(framework_spy)


def test_failed_correction_preserves_staged_work_and_records_termination(framework_spy, tmp_path):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return response(tool='submit_column_group', args=group())
        return response(tool='get_metadata', args={'probe_sequence': len(calls)})

    client = model(handler, stream=False)
    tools = Toolbox()
    path = tmp_path / 'incomplete.trace.jsonl'
    with bind_trace(RunTrace(path)):
        batch = client.explore(input_packet(), tools, max_steps=4, max_tool_calls=16)
    assert len(batch.proposals) == 1 and not batch._runtime_submission_complete
    assert batch._runtime_pending_codes == ['discovery_submission_incomplete', 'discovery_steps_exhausted']
    assert client.usage['calls'] == 4 and tools.executed == ['get_metadata']
    records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    end = next(entry['data'] for entry in records if entry['kind'] == 'agent_end')
    assert end['steps'] == 4 and end['termination_code'] == 'discovery_steps_exhausted'
    assert len(end['drafts']) == 1
    assert_cleared(framework_spy)


def test_early_source_allowance_exhaustion_has_only_one_correction(framework_spy):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return response(tool='get_metadata')

    client = model(handler, stream=False)
    tools = Toolbox()
    with pytest.raises(LightGraphError, match='discovery_final_response_required'):
        client.explore(input_packet(), tools, max_steps=8, max_tool_calls=1)
    assert len(requests) == client.usage['calls'] == 3
    assert tools.executed == ['get_metadata']
    assert runtime_feedback(requests[-1])['executed_tools'] == 0
    assert_cleared(framework_spy)


def test_phase_recovery_cannot_spend_an_exhausted_shared_call_budget(framework_spy):
    calls = []

    def handler(request):
        calls.append(1)
        return response(tool='get_metadata', args={'probe_sequence': len(calls)})

    budget = BudgetAllocator(Cost(llm_calls=7, llm_tokens=1000000,
                                  memory_bytes=1000000, seconds=60))
    client = model(handler, stream=False, budget=budget)
    tools = Toolbox()
    with pytest.raises(BudgetExceeded, match='llm_calls_budget_exhausted'):
        client.explore(input_packet(), tools, max_steps=8, max_tool_calls=16)
    assert len(calls) == client.usage['calls'] == 7
    assert tools.executed == ['get_metadata'] * 6
    assert budget.used.llm_calls == 7 and budget.snapshot()['reserved'] == {}
    assert_cleared(framework_spy)


@pytest.mark.parametrize('finish_tool', [False, True])
def test_valid_finish_ends_submission_without_a_corrective_call(finish_tool, framework_spy):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return response(tool='submit_column_group', args=group())
        if len(calls) == 2:
            return response(tool='get_metadata')
        assert len(calls) == 3
        return (response(tool='finish_discovery') if finish_tool else
                response({'action': 'finish_discovery', 'pending': False}))

    client = model(handler, stream=False)
    batch = client.explore(input_packet(), Toolbox(), max_steps=4)
    assert len(batch.proposals) == 1 and batch._runtime_submission_complete
    assert not batch._runtime_pending_codes
    assert client.usage['calls'] == 3
    assert_cleared(framework_spy)


def test_unstructured_text_uses_request_retry_then_finishes_existing_draft(framework_spy):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return response(tool='submit_column_group', args=group())
        if len(calls) == 2:
            return response(tool='get_metadata')
        if len(calls) == 3:
            return httpx.Response(200, json={'choices': [{'message': {
                'content': 'I have enough evidence and will finish now.'}, 'finish_reason': 'stop'}]})
        assert len(calls) == 4
        return response(tool='finish_discovery')

    client = model(handler, stream=False, retries=1)
    batch = client.explore(input_packet(), Toolbox(), max_steps=4)
    assert len(batch.proposals) == 1 and batch._runtime_submission_complete
    assert not batch._runtime_pending_codes
    assert client.usage['calls'] == 4 and client.usage['failures'] == 1
    assert client.usage['request_retry_succeeded'] == 1
    assert_cleared(framework_spy)
