"""Large support fan-in stays linear without deleting names or row evidence."""
from copy import deepcopy

from lightgraph.contracts import ColumnSpec, TableSpec, canonical
from lightgraph.construction.accumulation import GraphAccumulator
from lightgraph.construction.executor import execute_draft
from lightgraph.sources import MemorySource


def test_append_only_accounting_matches_canonical_payload_and_keeps_exact_values():
    payload = {'id': 'n1', 'label': '', 'aliases': [], 'properties': {}, 'provenance': [], 'supports': []}
    size, index_size = 0, 0
    def account(delta, index_delta):
        nonlocal size, index_size
        size += delta; index_size += index_delta
    accumulated = GraphAccumulator(account)
    accumulated.register(payload)
    for name, value, field in [('中文属性', '完整名字', 'properties'), ('位置', 3, 'properties'),
                              ('位置', 3, 'properties'), ('位置', True, 'properties'),
                              ('位置', 2.5, 'properties'), (None, '完整英文别名', 'aliases'),
                              (None, '完整英文别名', 'aliases'),
                              (None, {'row_context': {'values': {'a': 'A', 'b': None}}}, 'supports')]:
        accumulated.append(payload, field, value, property_name=name)
        assert size == len(canonical(payload).encode())
    accumulated.replace(payload, 'label', '正式名称')
    assert size == len(canonical(payload).encode()) and index_size > 0
    assert payload['properties']['位置'] == [3, True, 2.5]
    assert payload['aliases'] == ['完整英文别名']
    assert payload['supports'][0]['row_context']['values'] == {'a': 'A', 'b': None}
    # Long labels do not require copying the entire name into a second index.
    before = index_size
    assert not accumulated.label_conflicts('n1', 'public', 'long' * 4000)
    assert not accumulated.label_conflicts('n1', 'public', 'long' * 4000)
    assert accumulated.label_conflicts('n1', 'public', 'other')
    assert accumulated.label_conflicts('n1', 'public', 'long' * 4000)
    assert index_size - before < 1024


def support_case(count, *, max_working_bytes=32 * 1024 * 1024, duplicate=False, cancelled=None):
    table = TableSpec(source_id='s', name='paths', columns=[ColumnSpec(name=c)
        for c in ['parent', 'child', 'parent_name', 'child_name', 'event', 'attribute']])
    shared = {'source_id': 's', 'table': 'paths', 'context_columns': ['event', 'parent', 'child']}
    mappings = [{**shared, 'key_columns': [key], 'label_column': label, 'properties': {'value': 'attribute'},
        'constant_properties': {'position': position}} for key, label, position in [
            ('parent', 'parent_name', 1), ('child', 'child_name', 2)]]
    if duplicate:
        mappings += [deepcopy(mappings[0])]
    draft = {'draft_id': 'fan_in', 'entities': [{'type': 'Item', 'key_names': ['code'], 'mappings': mappings}],
        'relations': [{'type': 'parent_of', 'source_type': 'Item', 'target_type': 'Item', 'mappings': [
            {**shared, 'source_key_columns': ['parent'], 'target_key_columns': ['child'],
             'properties': {'value': 'attribute'}}]}]}
    # A→B and B→C both occur: B must be one object with supports at two positions.
    rows = [('A' if i % 2 == 0 else 'B', 'B' if i % 2 == 0 else 'C',
             '名称A' if i % 2 == 0 else '名称B', '名称B' if i % 2 == 0 else '名称C',
             f'E{i:06}', i % 3) for i in range(count)]
    with MemorySource([table], {table.qualified_name: rows}) as source:
        result = execute_draft(draft, [source], [table], max_working_bytes=max_working_bytes, cancelled=cancelled)
    return result


def test_many_supports_remain_complete_and_accounting_growth_is_linear():
    small = support_case(100)
    large = support_case(400)
    assert small['passed'] and large['passed'], large['diagnostics']
    assert large['metrics']['accumulation_serialized_bytes'] < small['metrics']['accumulation_serialized_bytes'] * 4.2
    graph = large['graph']; nodes = {node['identity']['code']: node for node in graph['nodes']}
    assert set(nodes) == {'A', 'B', 'C'} and len(graph['edges']) == 2
    assert nodes['B']['label'] == '名称B' and nodes['B']['properties']['position'] == [1, 2]
    assert nodes['B']['properties']['value'] == [0, 1, 2]
    assert len(nodes['B']['supports']) == 400
    assert sum(len(n['supports']) for n in graph['nodes']) == 800
    assert sum(len(e['supports']) for e in graph['edges']) == 400
    contexts = {support['row_context']['id'] for obj in graph['nodes'] + graph['edges'] for support in obj['supports']}
    assert len(contexts) == 400
    assert {s['row_context']['values']['event'] for s in nodes['B']['supports']} == {f'E{i:06}' for i in range(400)}
    assert large['metrics']['working_memory_exact_rss'] is False
    assert all(isinstance(value, (int, float, bool)) for value in large['metrics'].values())


def test_repeated_projection_retains_mapping_support_and_never_changes_fact_identity():
    normal = support_case(40)
    duplicate = support_case(40, duplicate=True)
    assert duplicate['passed']
    assert duplicate['metrics']['projection_cache_hits'] == 1
    assert {n['id'] for n in normal['graph']['nodes']} == {n['id'] for n in duplicate['graph']['nodes']}
    assert {e['id'] for e in normal['graph']['edges']} == {e['id'] for e in duplicate['graph']['edges']}
    # Identical source mapping at a different position in the plan adds a
    # separate provenance support; the optimizer must not discard it.
    assert sum(len(n['supports']) for n in duplicate['graph']['nodes']) == 120


def test_memory_failure_identifies_resource_and_mapping_without_source_values():
    failed = support_case(100, max_working_bytes=10000)
    assert not failed['passed']
    assert failed['graph']['nodes'] == failed['graph']['edges'] == []
    diagnostic = next(d for d in failed['diagnostics'] if d['code'] == 'memory_budget_exhausted')
    assert diagnostic['phase'] in {'projection', 'graph_accumulation'}
    assert diagnostic['source_id'] == 's' and diagnostic['entity_type'] == 'Item'
    assert diagnostic['limit_bytes'] == 10000
    assert diagnostic['estimated_required_bytes'] > diagnostic['limit_bytes']
    assert diagnostic['exact_rss'] is False
    assert '名称' not in canonical(diagnostic) and 'E000' not in canonical(diagnostic)


def test_cancelled_execution_stops_before_opening_snapshots_or_materializing():
    failed = support_case(100, cancelled=lambda: True)
    assert not failed['passed']
    assert failed['diagnostics'] == [{'code': 'construction_cancelled', 'retryable': False}]
    assert failed['source_read_windows'] == []
    assert failed['metrics']['mappings_checked'] == 0
    assert failed['graph']['nodes'] == failed['graph']['edges'] == []


def test_cancellation_during_projection_closes_snapshot_and_keeps_mapping_location():
    checks = 0
    def cancelled():
        nonlocal checks
        checks += 1
        return checks >= 12
    failed = support_case(100, cancelled=cancelled)
    assert not failed['passed']
    assert checks == 12
    diagnostic = failed['diagnostics'][0]
    assert diagnostic['code'] == 'construction_cancelled' and diagnostic['entity_type'] == 'Item'
    assert failed['source_read_windows'][0]['state'] == 'closed_after_interruption'
    assert failed['graph']['nodes'] == failed['graph']['edges'] == []
