"""A stale exploration call must not discard an otherwise publishable task."""
import json

from lightgraph.discovery.tools import ToolExecutor
from lightgraph.pipeline import run_pipeline
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import client, group, input_packet, table
from lightgraph.tests.test_proposal_session import tools_response


def test_submission_phase_correction_publishes_within_eight_calls(tmp_path, monkeypatch):
    source_table = table('items', ['item_code', 'item_name'])
    rows = {'public.items': [('A', '销售收入'), ('B', '销售成本')]}
    queries = [
        'SELECT DISTINCT item_code FROM public.items',
        'SELECT DISTINCT item_name FROM public.items',
        'SELECT COUNT(*) AS row_count FROM public.items',
        'SELECT item_code, COUNT(DISTINCT item_name) AS name_count FROM public.items GROUP BY item_code',
        'SELECT item_name, COUNT(DISTINCT item_code) AS code_count FROM public.items GROUP BY item_name',
        'SELECT item_code, item_name FROM public.items',
    ]
    stale_query = "SELECT item_code, item_name FROM public.items WHERE item_code = 'A'"
    requests, executed_queries = [], []
    execute_sql = ToolExecutor._execute_readonly_sql

    def tracked_sql(self, sql, *args, **kwargs):
        executed_queries.append(sql)
        return execute_sql(self, sql, *args, **kwargs)

    monkeypatch.setattr(ToolExecutor, '_execute_readonly_sql', tracked_sql)

    def handler(request):
        body, packet = input_packet(request)
        requests.append(body)
        turn = len(requests)
        assert packet['task'] in ('T0_discovery', 'T1_table_discovery')
        control = json.loads(body['messages'][1]['content'])['runtime_control']
        assert control['max_turns'] == 8
        if turn <= 6:
            assert not control['submission_required']
            return tools_response(('execute_readonly_sql', {'sql': queries[turn-1], 'max_rows': 2}))
        names = {spec['function']['name'] for spec in body['tools']}
        assert 'execute_readonly_sql' not in names
        assert {'submit_column_group', 'finish_discovery'} <= names
        if turn == 7:
            assert control['submission_reason'] == 'submission_window'
            assert executed_queries == queries
            return tools_response(('execute_readonly_sql', {'sql': stale_query, 'max_rows': 2}))
        assert turn == 8
        assert body['tool_choice'] == 'required'
        feedback = json.loads(body['messages'][-1]['content'])['runtime_feedback']
        assert feedback['code'] == 'source_tool_unavailable_during_submission'
        assert feedback['executed_tools'] == 0 and feedback['remaining_turns'] == 1
        assert executed_queries == queries
        proposal = group(packet, 'item_code', 'item_name')['proposals'][0]
        draft = {key: value for key, value in proposal.items() if key != 'kind'}
        return tools_response(('submit_column_group', draft), ('finish_discovery', {'pending': False}))

    model = client(monkeypatch, handler)
    model.config.max_discovery_steps = 8
    config = RunConfig(sources=[], output_dir=str(tmp_path), max_identity_reviews=0,
                       model={'mode': 'in_environment', 'max_discovery_steps': 8})
    with MemorySource([source_table], rows) as source:
        report = run_pipeline(config, [source], model=model)

    assert report.status == 'COMPLETED', report.issues
    assert len(requests) == model.usage['calls'] == 8
    assert model.usage['failures'] == 0
    assert model.usage['discovery_phase_rejections'] == model.usage['discovery_phase_recoveries'] == 1
    assert executed_queries == queries and stale_query not in executed_queries
    assert report.counts['discovered_rules'] == len(report.rules_published) == 1
    assert report.counts['business_nodes'] == 2
    assert not any(issue['code'] in ('discovery_steps_exhausted', 'unexplained_columns')
                   for issue in report.issues)
    assert model.budget.snapshot()['reserved'] == {}
    with GraphStore(tmp_path/'graph.sqlite') as store:
        assert len(store.get_rules()) == 1
        business = [node for node in store.view({'public'})['nodes'] if node['layer'] == 'business']
        assert {node['label'] for node in business} == {'销售收入', '销售成本'}

    traces = list((tmp_path/'traces').glob('*.trace.jsonl'))
    assert len(traces) == 1
    events = [json.loads(line) for line in traces[0].read_text(encoding='utf-8').splitlines()]
    sql_results = [json.loads(event['data']['content'][0]['text']) for event in events
                   if event['kind'] == 'tool_result' and event['data']['name'] == 'execute_readonly_sql']
    assert len(sql_results) == 6 and all(result['status'] == 'ok' for result in sql_results)
    phase_feedback = [event['data'] for event in events if event['kind'] == 'discovery_phase_feedback']
    assert len(phase_feedback) == 1
    assert phase_feedback[0]['code'] == 'source_tool_unavailable_during_submission'
    assert phase_feedback[0]['step'] == 7
    assert phase_feedback[0]['executed_tools'] == 0
    assert phase_feedback[0]['retry_available'] is True
