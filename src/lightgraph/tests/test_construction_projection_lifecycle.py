"""Count+projection share a read window; other mappings never keep it alive."""
from contextlib import contextmanager
from lightgraph.sources import MemorySource
from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.construction.executor import execute_draft
from lightgraph.tests.test_construction_endpoint_lookup import example, endpoint_pairs


class TrackedSource(MemorySource):
    def __init__(self, *args, activity, **kwargs):
        super().__init__(*args, **kwargs)
        self.activity = activity
        self.windows = []
        self.current = None

    @contextmanager
    def snapshot(self):
        # Includes cross-source overlap: no connection is held while another
        # source projection/lookup or CPU-side graph accumulation takes place.
        assert not self.activity
        self.activity.append(self)
        self.current = []
        self.windows.append(self.current)
        try:
            with super().snapshot():
                yield self
        finally:
            self.current = None
            assert self.activity.pop() is self

    def execute(self, query, **kwargs):
        assert self.activity == [self] and self.current is not None
        self.current.append(query)
        yield from super().execute(query, **kwargs)


def test_lookup_projections_release_both_sources_and_record_individual_windows():
    orders, customers, draft = example()
    activity = []
    with TrackedSource([orders], {orders.qualified_name: [('O1', 'a', 'public')]}, activity=activity) as a, TrackedSource(
        [customers], {customers.qualified_name: [('C1', 'a', '甲', 'public')]}, activity=activity) as b:
        result = execute_draft(draft, [a, b], [orders, customers])
        assert result['passed'], result['diagnostics']
        assert endpoint_pairs(result) == {('O1', 'C1')}
        assert not activity
        assert len(a.windows) == len(b.windows) == 2
        assert all(len(group) == 2 for group in a.windows + b.windows)
    windows = result['source_read_windows']
    assert len(windows) == 4
    assert all(w['state'] == 'closed' and w['completed_projections'] == 1 for w in windows)
    assert len({w['projection_fingerprint'] for w in windows}) == 4
    assert all(left['closed_at'] <= right['opened_at'] for left, right in zip(windows, windows[1:]))


def test_reused_complete_projection_does_not_reopen_or_extend_its_window():
    table = TableSpec(source_id='s', name='items', columns=[ColumnSpec(name=c) for c in ['code', 'label']])
    draft = {'draft_id': 'positions', 'entities': [{'type': 'Item', 'key_names': ['code'], 'mappings': [
        {'source_id': 's', 'table': 'items', 'key_columns': ['code'], 'label_column': 'label',
         'constant_properties': {'position': p}} for p in ['L1', 'L2']]}]}
    with TrackedSource([table], {table.qualified_name: [('A', '甲')]}, activity=[]) as source:
        result = execute_draft(draft, [source], [table])
        assert result['passed'] and result['metrics']['projection_cache_hits'] == 1
        assert len(source.windows) == 1 and len(source.windows[0]) == 2
        assert result['graph']['nodes'][0]['properties']['position'] == ['L1', 'L2']
        assert len(result['source_read_windows']) == 1


def test_unexpected_snapshot_error_does_not_abort_the_next_independent_source():
    class Broken(MemorySource):
        @contextmanager
        def snapshot(self):
            raise RuntimeError('PRIVATE_DRIVER_MESSAGE')
            yield self
    failed = TableSpec(source_id='bad', name='items', columns=[ColumnSpec(name='code')])
    healthy = failed.model_copy(update={'source_id': 'good'})
    draft = {'draft_id': 'isolated', 'entities': [
        {'type': source, 'key_names': ['code'], 'mappings': [
            {'source_id': source, 'table': 'items', 'key_columns': ['code']}]} for source in ['bad', 'good']]}
    with Broken([failed], {}) as bad, MemorySource([healthy], {healthy.qualified_name: [('A',)]}) as good:
        result = execute_draft(draft, [bad, good], [failed, healthy])
    assert not result['passed'] and len(result['graph']['nodes']) == 1
    assert result['graph']['nodes'][0]['type'] == 'good'
    assert result['diagnostics'][0]['source_id'] == 'bad'
    assert result['diagnostics'][0]['phase'] == 'snapshot_open'
    assert 'PRIVATE_DRIVER_MESSAGE' not in str(result)
