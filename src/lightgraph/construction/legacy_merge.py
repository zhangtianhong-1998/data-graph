"""Recompose the terminal output of a pre-checkpoint workflow from its own attempt."""
import re

from lightgraph.contracts import digest
from lightgraph.errors import LightGraphError
from .access import access_fingerprint, filter_graph_scopes
from .checkpoints import require_partial_isolation, usable_result
from .graph_storage import _graph_digest, read_graph_record
from .inheritance import frozen_contributors, inherited_semantics_digest
from .merger import merge_construction_graphs
from .workflow import build_merge_node_config, load_unit_contributor, unit_output_path
from .workspace import read_internal_yaml


def recompose_legacy_merge(config, old_output, source_run_id, node, outcome, worker, leaf_contributors):
    """Verify the run-specific upstream, frozen scope and committed delta before combining."""
    attempt = unit_output_path(old_output, node.node_id, source_run_id)
    root = attempt / 'construction'
    if any(path.is_symlink() for path in (attempt.parents[1], attempt.parent, attempt, root)):
        raise LightGraphError('workflow_legacy_merge_unavailable')

    worker_id = outcome['run_id']
    report_path = attempt / 'report.yaml'
    if report_path.is_symlink() or not report_path.is_file():
        raise LightGraphError('workflow_legacy_worker_unverified')
    report = read_internal_yaml(report_path)
    result_report = worker.get('report') or {}
    if (not isinstance(report, dict) or report.get('run_id') != worker_id or result_report.get('run_id') != worker_id
            or worker.get('status') != outcome.get('status') or not usable_result(worker)
            or outcome.get('error_code') not in {None, 'run_partial'}
            or report.get('status') != result_report.get('status')
            or result_report.get('status') != worker['status'].upper()
            or [i.get('code') for i in report.get('issues', [])] != [i.get('code') for i in result_report.get('issues', [])]):
        raise LightGraphError('workflow_legacy_worker_unverified')

    upstream_path = attempt / 'upstream.graph'
    if not upstream_path.is_dir():
        raise LightGraphError('workflow_legacy_upstream_unverified')
    upstream = read_graph_record(upstream_path)
    project_access = access_fingerprint(config)
    if (upstream.get('status') != 'published' or upstream.get('target') != 'graph'
            or upstream.get('access_fingerprint') != project_access):
        raise LightGraphError('workflow_legacy_upstream_unverified')
    policy = (node.merge_policy or config.workflow.merge_policy).model_copy(update={'fusion_policy': None})
    rebuilt = merge_construction_graphs(leaf_contributors, target_access_fingerprint=project_access,
                                        recognize_source_replays=False,
                                        merge_policy=policy.model_copy(update={'emit_alignment_candidates': False}))
    # Alignment diagnostics have evolved across releases. Compare the emitted facts
    # themselves, including each support, before accepting the original snapshot.
    if rebuilt['nodes'] != upstream['nodes'] or rebuilt['edges'] != upstream['edges']:
        raise LightGraphError('workflow_legacy_upstream_unverified')
    del rebuilt
    scoped = build_merge_node_config(config, node, upstream_snapshot_path=upstream_path,
                                     upstream_summary='', node_output_dir=attempt)
    frozen, summary = frozen_contributors(root, config=scoped)
    ref = scoped.inherited_graphs[0]
    if len(frozen) != 1 or len(summary['inherited']) != 1:
        raise LightGraphError('workflow_legacy_upstream_unverified')
    inherited = summary['inherited'][0]
    visible = filter_graph_scopes(upstream, ref.allowed_scopes)
    if inherited.get('source_digest_version') == 2:
        source_digest = inherited_semantics_digest(visible)
    elif (inherited.get('source_digest_version', 1) == 1
            and not visible.get('identity_separations') and not frozen[0]['graph'].get('identity_separations')):
        # Original pre-checkpoint snapshots can only establish the old contract
        # when neither verified artifact contains unpinned identity constraints.
        source_digest = digest({key: visible[key] for key in ('nodes', 'edges', 'definitions')})
    else:
        raise LightGraphError('workflow_legacy_upstream_unverified')
    if (frozen[0]['contributor_id'] != ref.inherit_id or inherited.get('source_access') != project_access
            or set(inherited.get('allowed_scopes', [])) != set(ref.allowed_scopes)
            or inherited.get('source_graph_digest') != source_digest or not inherited.get('frozen_digest')):
        raise LightGraphError('workflow_legacy_upstream_unverified')
    visible_ids = {n['id'] for n in visible['nodes']}
    del visible, frozen

    manifest_path = root / 'manifest.yaml'
    if manifest_path.is_symlink() or not manifest_path.is_file() or (root / 'published').is_symlink():
        raise LightGraphError('workflow_legacy_delta_unverified')
    manifest = read_internal_yaml(manifest_path)
    head = manifest.get('current_graph') or {}
    delta_path = head.get('path', '')
    if (not re.fullmatch(r'published/[0-9a-f]{32}\.graph', delta_path)
            or any(entry.get('graph') == delta_path for entry in manifest.get('publications', {}).values())
            or (root / delta_path).is_symlink() or head.get('run_id') not in {None, worker_id}
            or head.get('access_fingerprint') not in {None, access_fingerprint(scoped)}):
        raise LightGraphError('workflow_legacy_delta_unverified')
    delta = load_unit_contributor(attempt, node.node_id)
    if not delta:
        raise LightGraphError('workflow_legacy_delta_unverified')
    graph = delta['graph']
    if (graph.get('status') != 'published' or graph.get('target') != 'graph'
            or graph.get('access_fingerprint') != access_fingerprint(scoped)):
        raise LightGraphError('workflow_legacy_delta_unverified')
    counts = result_report.get('counts', {})
    if 'delta_nodes' in counts or 'delta_edges' in counts:
        visible_delta = filter_graph_scopes(graph, scoped.scopes, external_visible_ids=visible_ids)
        if any(counts.get('delta_' + key) != len(visible_delta[key]) for key in ('nodes', 'edges')):
            raise LightGraphError('workflow_legacy_delta_unverified')
        del visible_delta
    require_partial_isolation(worker, graph)

    # Preserve the original exact-key, identity-domain and conflict policy. New
    # project defaults may guide future extraction but cannot reinterpret this run.
    combined = merge_construction_graphs([
        {'kind': 'base', 'contributor_id': f'upstream_{node.node_id}', 'graph': upstream}, delta
    ], target_access_fingerprint=project_access, merge_policy=policy, recognize_source_replays=False)
    proof = {'run_id': source_run_id, 'worker_run_id': worker_id,
             'upstream_graph_digest': _graph_digest(upstream), 'frozen_graph_digest': inherited['frozen_digest'],
             'delta_path': delta_path, 'delta_graph_digest': _graph_digest(graph),
             'merge_policy': policy.model_dump(mode='json', exclude={'fusion_policy'})}
    return {'kind': 'merged', 'contributor_id': node.node_id, 'graph': combined,
            'evidence_records': delta.get('evidence_records', {})}, proof
