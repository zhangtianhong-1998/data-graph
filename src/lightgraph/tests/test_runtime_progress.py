"""Telemetry is bounded, process-isolated and cannot change extraction results."""
import json

from lightgraph.runtime.progress import (MAX_EVENTS, MAX_SNAPSHOT_BYTES, RunProgress,
    bind_progress, emit_progress, finish_progress, progress_path, read_progress)

RID = 'run_' + 'a' * 24


def test_fixed_events_and_counts_are_safe_and_bounded(tmp_path):
    path = tmp_path / (RID + '.progress.json')
    sink = RunProgress(path)
    with bind_progress(sink):
        emit_progress('discovery', 'stage_started', completed=0, total=10, unit='tables')
        emit_progress('discovery', 'SOURCE_ROW_SECRET')
        for index in range(MAX_EVENTS + 15):
            emit_progress(None, 'model_call_completed', table='fruit.public.指标',
                          counters={'model_calls': index + 1, 'api_key': 'SOURCE_ROW_SECRET',
                                    'input_tokens': 'SOURCE_ROW_SECRET', 'output_tokens': -1})
        # Arbitrary subprocess output is never part of the event schema.
        sink({'stage': 'discovery', 'code': 'issue_detected', 'message': 'SOURCE_ROW_SECRET',
              'rows': ['SOURCE_ROW_SECRET'], 'level': 'SOURCE_ROW_SECRET', 'table': '<SOURCE_ROW_SECRET>'})
    raw = path.read_bytes()
    assert len(raw) <= MAX_SNAPSHOT_BYTES and b'SOURCE_ROW_SECRET' not in raw
    value = read_progress(path, RID)
    assert len(value['events']) == MAX_EVENTS
    assert value['dropped_events'] == 17
    assert value['counters'] == {'model_calls': MAX_EVENTS + 15}
    assert (value['stage'], value['completed'], value['total']) == ('discovery', 0, 10)
    assert value['events'][-1]['level'] == 'info'


def test_phase_change_and_failure_never_invent_completion(tmp_path):
    path = tmp_path / (RID + '.progress.json')
    with bind_progress(RunProgress(path)):
        emit_progress('extraction', 'rule_completed', completed=3, total=5, unit='rules')
        emit_progress('cleanup', 'cleanup_started')
        emit_progress('finished', 'run_partial', level='warning')
    value = read_progress(path, RID)
    assert value['stage'] == 'finished'
    assert value['completed'] is None and value['total'] is None
    assert value['events'][0]['completed'] == 3
    assert value['events'][-1]['code'] == 'run_partial'


def test_bad_telemetry_does_not_escape_or_leak_bindings(tmp_path, monkeypatch):
    events = []
    with bind_progress(events.append):
        emit_progress('metadata', 'stage_started')
        with bind_progress(lambda _: (_ for _ in ()).throw(OSError('SOURCE_ROW_SECRET'))):
            emit_progress('metadata', 'stage_completed')
        emit_progress('validation', 'stage_started')
    emit_progress('finished', 'run_completed')
    assert [entry['stage'] for entry in events] == ['metadata', 'validation']
    monkeypatch.setattr('lightgraph.runtime.progress.atomic_text', lambda *_: (_ for _ in ()).throw(OSError()))
    with bind_progress(RunProgress(tmp_path / (RID + '.progress.json'))):
        emit_progress('initializing', 'run_started')


def test_reader_rejects_foreign_corrupt_oversized_and_symlinked_snapshots(tmp_path):
    path = tmp_path / (RID + '.progress.json')
    assert read_progress(path, RID) is None
    with bind_progress(RunProgress(path)):
        emit_progress('initializing', 'run_started')
    assert read_progress(path, 'run_' + 'b' * 24) is None
    original = path.read_text()
    parsed = json.loads(original)
    parsed['events'][0]['code'] = 'SOURCE_ROW_SECRET'
    path.write_text(json.dumps(parsed))
    assert read_progress(path, RID) is None
    path.write_text('x' * (MAX_SNAPSHOT_BYTES + 1))
    assert read_progress(path, RID) is None
    path.write_text('{')
    assert read_progress(path, RID) is None
    path.write_text(original)
    try:
        link = tmp_path / 'link.json'
        link.symlink_to(path)
    except OSError:
        pass  # Creating symlinks requires extra privileges on some Windows hosts.
    else:
        assert read_progress(link, RID) is None


def test_reader_strips_unknown_fields_from_valid_file(tmp_path):
    path = tmp_path / (RID + '.progress.json')
    with bind_progress(RunProgress(path)):
        emit_progress('discovery', 'model_call_started', counters={'model_calls': 1})
    raw = json.loads(path.read_text())
    raw['password'] = 'SOURCE_ROW_SECRET'
    raw['counters']['password'] = 'SOURCE_ROW_SECRET'
    raw['events'][0]['message'] = 'SOURCE_ROW_SECRET'
    path.write_text(json.dumps(raw))
    assert 'SOURCE_ROW_SECRET' not in json.dumps(read_progress(path, RID))


def test_parent_appends_authoritative_cancel_without_overwriting_history(tmp_path):
    path = tmp_path / (RID + '.progress.json')
    with bind_progress(RunProgress(path)):
        emit_progress('discovery', 'table_started', completed=1, total=5, unit='tables')
    finish_progress(path, RID, 'cancelled')
    value = read_progress(path, RID)
    assert [e['code'] for e in value['events']] == ['table_started', 'run_cancelled']
    assert value['stage'] == 'finished' and value['completed'] is None
    finish_progress(path, RID, 'cancelled')
    assert len(read_progress(path, RID)['events']) == 2


def test_progress_paths_are_siblings_and_result_suffix_is_removed(tmp_path):
    record = tmp_path / (RID + '.json')
    result = tmp_path / (RID + '.result.json')
    assert progress_path(record) == progress_path(result) == tmp_path / (RID + '.progress.json')


def test_new_batch_in_same_stage_resets_counts_but_model_events_keep_them(tmp_path):
    path = tmp_path / (RID + '.progress.json')
    with bind_progress(RunProgress(path)):
        emit_progress('discovery', 'table_completed', completed=5, total=5, unit='tables')
        emit_progress(None, 'model_call_started')
        assert read_progress(path, RID)['completed'] == 5
        emit_progress('discovery', 'stage_started')
        assert read_progress(path, RID)['completed'] is None
        assert read_progress(path, RID)['total'] is None
        emit_progress('discovery', 'stage_started', completed=0, total=2, unit='tables')
        emit_progress(None, 'model_call_completed')
        assert read_progress(path, RID)['completed'] == 0
        assert read_progress(path, RID)['total'] == 2
