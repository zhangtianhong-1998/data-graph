"""Exercise large synthetic graph publication and merge without a model or database.

Run: .venv/bin/python scripts/benchmark_graph_storage.py --size-mib 140
Temporary snapshots are removed after the run. This is a storage regression,
not a business extraction or database accuracy evaluation.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
import time

from lightgraph.construction.merger import merge_construction_graphs, write_sharded_graph
from lightgraph.construction.workflow import load_unit_contributor
from lightgraph.construction.workspace import GraphWorkspace
from lightgraph.contracts import digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--size-mib', type=int, default=140)
    args = parser.parse_args()
    if args.size_mib < 1:
        parser.error('--size-mib must be positive')
    # Payload alone exceeds the historical 128 MiB YAML bound at the default.
    payload = 'source-observation-' * 432
    count = (args.size_mib * 1024 * 1024 + len(payload) - 1) // len(payload)
    graph = {
        'format_version': 1, 'target': 'graph', 'status': 'validated',
        'definitions': {'entities': [{'type': 'Item', 'key_names': ['code']}], 'relations': []},
        'nodes': [{'id': f'n{i}', 'type': 'Item', 'identity': {'code': str(i)},
                   'label': f'Item {i}', 'properties': {'observation': [payload]},
                   'supports': [{'scope_ref': 'public', 'label': f'Item {i}'}]}
                  for i in range(count)],
        'edges': [], 'evidence_refs': [],
    }
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='lightgraph-storage-') as temporary:
        root = Path(temporary)
        ws = GraphWorkspace(root / 'unit' / 'construction', 'ctx')
        ws.save_draft({'draft_id': 'items', 'entities': [
            {'type': 'Item', 'key_names': ['code'], 'mappings': [
                {'source_id': 'synthetic', 'table': 'items', 'key_columns': ['code']}]}]})
        record = ws.record_validation('items', revision=1, passed=True, graph=graph)
        assert digest(ws.read_validation_graph('items', record.validation)) == digest(graph)
        ws.publish('items', revision=1, graph=graph)
        contributor = load_unit_contributor(root / 'unit', 'unit')
        assert len(contributor['graph']['nodes']) == count
        merged = merge_construction_graphs([contributor])
        publication = write_sharded_graph(root / 'merged', merged, context_fingerprint='merged')
        loaded = GraphWorkspace(root / 'merged', 'merged').read_published_graph()
        assert loaded == {**merged, 'storage_layout': 'parquet_v1'}
        files = list((root / 'merged' / publication['core_path']).iterdir())
        print(json.dumps({
            'kind': 'synthetic_storage_regression', 'nodes': count,
            'payload_bytes': count * len(payload),
            'published_snapshot_bytes': sum(file.stat().st_size for file in files),
            'seconds': round(time.monotonic() - started, 3),
            'preview_publish_contributor_merge_reload': 'passed',
        }, indent=2))


if __name__ == '__main__':
    main()
