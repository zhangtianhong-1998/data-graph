"""Capture reproducible code, configuration and artifact hashes without reading database rows."""
import argparse
import json
import os
from pathlib import Path
import tempfile

from lightgraph.errors import LightGraphError
from lightgraph.runtime.run_baseline import capture_baseline


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, help='Run configuration YAML (config.yaml for a workbench project)')
    parser.add_argument('--output', required=True, help='New JSON manifest; existing files are never replaced')
    parser.add_argument('--repo', help='Source checkout; defaults to this installation')
    parser.add_argument('--frontend', help='Built frontend directory; defaults to src/workbench/dist')
    parser.add_argument('--run-id', help='Optional experiment or run identifier')
    args = parser.parse_args()
    destination = Path(args.output)
    if destination.exists() or destination.is_symlink():
        parser.exit(2, 'baseline_output_exists\n')
    try:
        result = capture_baseline(args.config, repo=args.repo, frontend=args.frontend, run_id=args.run_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.baseline-', dir=destination.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
    except (LightGraphError, OSError, ValueError) as error:
        parser.exit(2, getattr(error, 'code', 'baseline_capture_failed') + '\n')
    print(json.dumps({'baseline_id': result['baseline_id'], 'output': str(destination.resolve()),
                      'source_files': len(result['source']['files']), 'inputs': len(result['inputs']),
                      'checkpoints': len(result['artifacts']['checkpoints'])}, ensure_ascii=False))


if __name__ == '__main__':
    main()
