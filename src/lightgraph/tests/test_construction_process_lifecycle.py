"""A killed YAML writer cannot expose a partial publication or lose prior work."""
import subprocess
import sys
import time

from lightgraph.construction.workspace import GraphWorkspace
from lightgraph.tests.test_graph_yaml_workspace import graph, payload


def test_process_killed_before_manifest_replace_keeps_old_publication(tmp_path):
    root = tmp_path / 'workspace'
    store = GraphWorkspace(root, 'ctx')
    for draft_id, node_id in [('first', 'n1'), ('second', 'n2')]:
        store.save_draft(payload(draft_id))
        store.record_validation(draft_id, revision=1, passed=True, graph=graph(node_id))
    store.publish('first', revision=1, graph=graph('n1'))
    before = store.manifest_path.read_bytes()
    marker = tmp_path / 'ready'
    script = '''
from pathlib import Path
import os, sys, time
from lightgraph.construction.workspace import GraphWorkspace
from lightgraph.tests.test_graph_yaml_workspace import graph
root, marker = map(Path, sys.argv[1:])
store = GraphWorkspace(root, 'ctx')
replace = os.replace
def pause_before_commit(source, target):
    if Path(target) == store.manifest_path:
        marker.write_text('pending replacement', encoding='utf-8')
        time.sleep(60)
    return replace(source, target)
os.replace = pause_before_commit
store.publish('second', revision=1, graph=graph('n2'))
'''
    process = subprocess.Popen([sys.executable, '-c', script, str(root), str(marker)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 20
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert marker.exists(), process.communicate(timeout=1)[1].decode() if process.poll() is not None else 'writer did not reach commit'
        process.kill()
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        process.stderr.close()
    assert store.manifest_path.read_bytes() == before
    resumed = GraphWorkspace(root, 'ctx')
    assert [node['id'] for node in resumed.read_published_graph()['nodes']] == ['n1']
    assert resumed.get_draft('second').status == 'validated'
    resumed.publish('second', revision=1, graph=graph('n2'))
    assert [node['id'] for node in resumed.read_published_graph()['nodes']] == ['n1', 'n2']
