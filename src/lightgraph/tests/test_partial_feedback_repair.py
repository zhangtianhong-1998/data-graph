"""Independent repair outcomes must remain independent in the work ledger."""
from lightgraph.pipeline import run_pipeline
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import client, group, input_packet, response, table


def test_one_repaired_rule_does_not_resolve_sibling_no_progress(monkeypatch, tmp_path):
    source_table = table('multi', ['x', 'x_name', 'y', 'y_name', 'realm'])
    calls = []

    def handler(request):
        _, packet = input_packet(request)
        keys = (packet.get('revision_candidate') or {}).get('identity', {}).get('key_columns', [])
        calls.append((packet['task'], keys))
        if packet['task'] == 'T0_discovery':
            batch = group(packet, 'x', 'x_name', 'Metric')
            batch['proposals'] += group(packet, 'y', 'y_name', 'Measure')['proposals']
            return response(batch)
        if packet['task'] == 'T5_revision' and 'x' in keys:
            return response(group(packet, 'x', 'x_name', 'Metric', namespace='realm'))
        return response({'task': packet['task'], 'abstain': True})

    config = RunConfig(sources=[], output_dir=str(tmp_path), scopes=['public', 'A', 'B'],
                       max_identity_reviews=0)
    original = [('X', 'x-a', 'Y', 'y-a', 'A')]
    rows = original + [('X', 'x-b', 'Y', 'y-b', 'B')]
    with MemorySource([source_table], {'public.multi': original}) as source:
        run_pipeline(config, [source], model=client(monkeypatch, handler))
    calls.clear()
    with MemorySource([source_table], {'public.multi': rows}) as source:
        repaired = run_pipeline(config, [source], model=client(monkeypatch, handler))
    assert repaired.counts['business_nodes'] == 2
    assert sum(task == 'T5_revision' for task, _ in calls) == 2
    with GraphStore(tmp_path / 'graph.sqlite') as store:
        rules = {r.rule_id: r for r in store.get_rules(include_stale=True)}
        work = list(store.connection.execute("SELECT * FROM iteration_work WHERE task='review_pattern'"))
        import json
        states = {rules[next(iter(json.loads(item['rule_revisions'])))].identity.entity_kind: item['state']
                  for item in work}
        assert states == {'Metric': 'resolved', 'Measure': 'unresolved'}
        assert not [n for n in store.view({'public', 'A', 'B'})['nodes'] if n['kind'] == 'Measure']
    calls.clear()
    with MemorySource([source_table], {'public.multi': rows}) as source:
        repeated = run_pipeline(config, [source], model=client(monkeypatch, handler))
    assert repeated.counts['business_nodes'] == 2
    assert not [task for task, _ in calls if task == 'T5_revision']
