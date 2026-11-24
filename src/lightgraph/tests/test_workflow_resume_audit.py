"""A copied legacy run can be diagnosed without starting a run or reading graph rows."""
from pathlib import Path
import runpy
import shutil

from lightgraph.construction import graph_storage
from lightgraph.tests.test_workflow_input_relocation import archived_csv_workflow


def test_diagnosis_matches_the_origin_and_detects_changed_file(archived_csv_workflow, tmp_path, monkeypatch):
    original, run = archived_csv_workflow
    moved = tmp_path / 'diagnostics' / 'alpha'
    shutil.copytree(original, moved)
    run_file = moved / 'runs' / (run['run_id'] + '.json')
    audit = runpy.run_path(str(Path(__file__).resolve().parents[3] / 'scripts/audit_workflow_resume.py'))['audit']

    def no_graph_reads(*args):
        raise AssertionError('input diagnosis must not materialize a checkpoint')

    monkeypatch.setattr(graph_storage, '_read_rows', no_graph_reads)
    before = {p.relative_to(moved): p.stat().st_mtime_ns for p in moved.rglob('*') if p.is_file()}
    missing = audit(run_file, 'join')
    assert not missing['all_upstream_inputs_and_access_match']
    assert {n['result'] for n in missing['upstream_inputs']} == {'workflow_resume_input_changed'}
    match = audit(run_file, 'join', str(original))
    assert match['all_upstream_inputs_and_access_match']
    assert {n['matched_as'] for n in match['upstream_inputs']} == {'v1_historical_root'}
    assert {p.relative_to(moved): p.stat().st_mtime_ns for p in moved.rglob('*') if p.is_file()} == before

    csv, = (moved / 'files').glob('*.csv')
    csv.write_bytes(csv.read_bytes() + b'changed')
    changed = audit(run_file, 'join', str(original))
    assert not changed['all_upstream_inputs_and_access_match']
    assert changed['file_manifest_comparison'] == {'added': 0, 'removed': 0, 'changed': 1}
