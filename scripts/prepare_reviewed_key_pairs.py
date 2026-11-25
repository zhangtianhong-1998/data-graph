"""Preview a pair of physical projections, or compile an existing business approval for those checkpoints."""
import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from lightgraph.construction.checkpoints import load_checkpoint
from lightgraph.construction.reviewed_keys import _observations, _physical_code, reviewed_observation_digest
from lightgraph.construction.workflow import load_workflow_state, run_output_path
from lightgraph.contracts import canonical, digest
from lightgraph.runtime.config import ReviewedKeyPairRule, ReviewedKeyProjection


def _read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def _project(contributor, family, binding):
    """Use the same typed evidence check as the merger, with the checkpoint's verified owner."""
    graph = contributor['graph']
    definitions = {d['type']: d for d in graph['definitions']['entities']}
    matches = []
    codes = Counter()
    for node in graph['nodes']:
        if node.get('original_type', node['type']) != family:
            continue
        supports = [{'contributor_id': binding.contributor_id, **s} for s in node.get('supports', [])]
        node = {**node, 'supports': supports}
        code = _physical_code(node, binding)
        if code is None:
            continue
        codes[code] += 1
        observed = _observations(node, definitions[node['type']], binding)
        if observed is None:
            continue
        key, = node['identity']
        s = supports[0]
        matches.append((code, observed, {'key_type': node['identity_types'][key], 'key': node['identity'][key],
                                        'name': s.get('observed_label') or s['label']}))
    return {observed: row for code, observed, row in matches if codes[code] == 1}


def prepare(run_file, node_id, spec, approval=None, *, simulate=False):
    run_file = Path(run_file)
    run = _read(run_file)
    if run_file.parent.name != 'runs' or run.get('run_id') != run_file.stem:
        raise ValueError('请选择 runs 目录中与运行 ID 一致的 JSON 文件。')
    project = run_file.parent.parent
    state = load_workflow_state(run_output_path(project, run['output_path']), run['run_id'])
    if not state or state.get('parent_run_id') != run['run_id']:
        raise ValueError('找不到该运行的归档状态。')
    target = state['artifacts'][node_id]
    if target['run_id'] != run['run_id'] or target.get('artifact_status') != 'committed':
        raise ValueError('请选择实际提交该合并产物的运行。')
    inputs = {r['node_id']: r for r in state['artifacts'].values() if r['artifact_id'] in target['upstream_artifacts']}
    family = spec['entity_type']
    projections = spec['projections']
    if len(projections) != 2 or projections[0]['contributor_id'] == projections[1]['contributor_id']:
        raise ValueError('需要两个不同上游抽取单元的物理绑定。')
    sides, bindings = [], []
    for p in projections:
        source = p['contributor_id']
        record = inputs[source]
        if (record.get('artifact_status') != 'committed' or record.get('contributor_kind') != 'unit'
                or record.get('inherited_grants') or target.get('inherited_grants')):
            raise ValueError('需要两个直接提交的抽取检查点。')
        location = state.get('artifact_outputs', {}).get(source, run['output_path'])
        contributor = load_checkpoint(run_output_path(project, location), record)
        binding = ReviewedKeyProjection.model_validate({**p, 'graph_digest': record['graph_digest']})
        bindings.append(binding.model_dump(mode='json', by_alias=True))
        sides.append(_project(contributor, family, binding))
    excluded = {canonical(key) for key in spec.get('excluded_keys', [])}
    candidates = {}
    for key in sorted(sides[0].keys() & sides[1].keys(), key=canonical):
        a, b = sides[0][key], sides[1][key]
        if a == b and canonical(a['key']) not in excluded:
            candidates.setdefault(key[0], []).append(a)
    candidates = {token: rows[0] for token, rows in candidates.items() if len(rows) == 1}
    result = {'mode': 'preview', 'run_id': run['run_id'], 'project_id': run['project_id'], 'node_id': node_id, 'merge_artifact_id': target['artifact_id'],
              'entity_type': family, 'projections': bindings, 'candidate_count': len(candidates),
              'observations': sorted(candidates.values(), key=canonical)}
    if simulate:
        if approval is not None:
            raise ValueError('模拟预览与业务批准必须分别运行。')
        approval = {'approval_ref': 'simulation_only', 'preview_digest': digest(result), 'observations': result['observations']}
    if approval is not None:
        if not simulate and str(approval.get('approval_ref', '')).strip() == 'simulation_only':
            raise ValueError('内存模拟不能作为业务批准记录。')
        if approval.get('preview_digest') != digest(result):
            raise ValueError('批准记录对应的预览摘要与当前检查点不一致。')
        approved = []
        for row in approval['observations']:
            token = reviewed_observation_digest(row['key_type'], row['key'], row['name'])
            if token not in candidates or canonical(candidates[token]) != canonical(row):
                raise ValueError('批准清单中的编码、类型或名称未通过双侧原始观测核对。')
            approved.append(token)
        rule = ReviewedKeyPairRule(approval_ref=approval['approval_ref'], entity_type=family,
                                   projections=bindings, observation_digests=sorted(approved))
        field = 'simulated_key_pairs' if simulate else 'reviewed_key_pairs'
        return {'mode': 'simulation_only' if simulate else 'compiled', 'version': 1, 'run_id': run['run_id'], 'project_id': run['project_id'],
                'node_id': node_id, 'merge_artifact_id': target['artifact_id'],
                'upstream_artifacts': target['upstream_artifacts'], ('simulated_count' if simulate else 'approved_count'): len(approved),
                field: [rule.model_dump(mode='json', by_alias=True)]}
    return {**result, 'preview_digest': digest(result)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--node', default='merge_1')
    parser.add_argument('--spec', type=Path, required=True,
                        help='JSON with entity_type, two projections and optional excluded_keys.')
    decision = parser.add_mutually_exclusive_group()
    decision.add_argument('--simulate', action='store_true', help='Replay all matching preview rows in memory only. No business approval is recorded.')
    decision.add_argument('--approval', type=Path,
                        help='Business record with approval_ref, preview_digest and approved observations. Omit to preview.')
    parser.add_argument('--output', type=Path, required=True, help='New JSON file, UTF-8.')
    args = parser.parse_args()
    report = prepare(args.run, args.node, _read(args.spec), _read(args.approval) if args.approval else None, simulate=args.simulate)
    with args.output.open('x', encoding='utf-8') as out:
        json.dump(report, out, ensure_ascii=False, indent=2)
        out.write('\n')


if __name__ == '__main__':
    main()
