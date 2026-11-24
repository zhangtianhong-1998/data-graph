"""Explain the input fingerprints for a merge-only resume without starting a run."""
import argparse
import json
from pathlib import Path

from lightgraph.construction.checkpoints import verify_checkpoint_files
from lightgraph.construction.workflow import build_unit_config, load_workflow_state, resolve_workflow_plan, run_output_path
from lightgraph.errors import LightGraphError
from lightgraph.web.execution import _WorkflowInputProofs, _check_reused_access, _input_manifest
from lightgraph.web.workspace import Workspace


def audit(run_file, node_id, previous_project_root=None):
    run_file = Path(run_file).resolve()
    project = run_file.parent.parent
    run = json.loads(run_file.read_text(encoding='utf-8'))
    if (run_file.parent.name != 'runs' or run.get('run_id') != run_file.stem
            or run.get('project_id') != project.name or not run.get('workflow')):
        raise ValueError('请选择项目 runs 目录中的工作流运行记录。')
    state = load_workflow_state(run_output_path(project, run['output_path']), run['run_id'])
    if not state or state.get('parent_run_id') != run['run_id'] or not state.get('node_inputs'):
        raise ValueError('没有找到属于该运行的节点输入摘要。')
    config = Workspace(project.parent).resolved_config(project.name)
    if not config.workflow or not config.workflow.enabled:
        raise ValueError('当前项目没有启用工作流。')
    plan = resolve_workflow_plan(config.workflow)
    parents = plan.merge_inputs.get(node_id, [])
    units = {u.unit_id: u for u in config.workflow.units}
    if not parents or config.inherited_graphs or any(p not in units for p in parents):
        raise ValueError('此核查用于直接合并抽取检查点的节点。')
    files = _input_manifest(config, project)
    historical_files = run.get('dependencies')
    # Hash all current inputs once. Only the dependency spelling changes in a v1 counterfactual.
    proofs = _WorkflowInputProofs(config, project, files)
    inputs = []
    for nid in parents:
        archived = state['node_inputs'].get(nid)
        row = {'node_id': nid, 'archived_digest': archived, 'v2_current': proofs.current[nid],
               'v1_current_root': proofs.legacy_at(None)[nid]}
        if previous_project_root:
            row['v1_previous_root'] = proofs.legacy_at(previous_project_root)[nid]
        inputs.append(row)
        record = state.get('artifacts', {}).get(nid)
        if not record:
            row['result'] = 'workflow_checkpoint_missing'
            continue
        row.update(artifact_id=record['artifact_id'], checkpoint_digest=record.get('input_digest'))
        try:
            output = run_output_path(project, state.get('artifact_outputs', {}).get(nid, run['output_path']))
            verify_checkpoint_files(output, record)
            if record.get('node_id') != nid or record.get('graph_kind') != 'node_delta':
                raise LightGraphError('workflow_checkpoint_invalid')
            if not archived or archived != record.get('input_digest'):
                row['result'] = 'archive_checkpoint_digest_mismatch'
                continue
            origins = [None, state.get('legacy_node_input_roots', {}).get(nid), previous_project_root]
            legacy_root = proofs.match(nid, archived, record, origins)
            row['matched_as'] = 'v2' if legacy_root is None else (
                'v1_current_root' if archived == row['v1_current_root'] else 'v1_historical_root')
            scoped = build_unit_config(config, units[nid], unit_output_dir=output)
            _check_reused_access(output / 'construction', record, scoped, project, legacy_root)
            row['result'] = 'inputs_and_access_match'
        except LightGraphError as exc:
            row['result'] = exc.code
    manifest_comparison = None
    if isinstance(historical_files, dict):
        manifest_comparison = {
            'added': len(files.keys() - historical_files.keys()),
            'removed': len(historical_files.keys() - files.keys()),
            'changed': sum(files[p] != historical_files[p] for p in files.keys() & historical_files.keys()),
        }
    return {'run_id': run['run_id'], 'project_root': str(project), 'node_to_rerun': node_id,
            'previous_project_root': previous_project_root, 'file_manifest_comparison': manifest_comparison,
            'upstream_inputs': inputs,
            'all_upstream_inputs_and_access_match': all(r['result'] == 'inputs_and_access_match' for r in inputs)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_file', type=Path)
    parser.add_argument('--node', default='merge_1')
    parser.add_argument('--previous-project-root', help='归档时的项目绝对路径，仅用于重算历史摘要')
    args = parser.parse_args()
    try:
        report = audit(args.run_file, args.node, args.previous_project_root)
    except (LightGraphError, KeyError, OSError, ValueError) as exc:
        parser.exit(1, f'无法完成输入核对：{getattr(exc, "code", str(exc))}\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['all_upstream_inputs_and_access_match'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
