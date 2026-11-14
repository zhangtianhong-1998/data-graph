"""In-project parallel workflow unit builder, merger orchestrator, and state persistence."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from pathlib import Path
from typing import Any

from lightgraph.contracts import digest
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import RunConfig, WorkflowUnitConfig
from lightgraph.runtime.retention import atomic_text
from .access import access_fingerprint
from .inheritance import freeze_inherited_graphs, frozen_contributors
from .intent import effective_goal_markdown
from .merger import merge_construction_graphs, write_sharded_graph
from .workspace import GraphWorkspace, safe_load_yaml, write_yaml


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def unit_output_path(project_output_dir: str | Path, unit_id: str, attempt: str | None = None) -> Path:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', unit_id):
        raise LightGraphError('workflow_unit_not_found')
    if attempt is None:
        return Path(project_output_dir) / 'units' / unit_id
    if not re.fullmatch(r'run_[a-f0-9]{24}', attempt):
        raise LightGraphError('invalid_workflow_attempt')
    return Path(project_output_dir) / 'attempts' / attempt / unit_id


def published_unit_attempts(project_output_dir):
    manifest_path = Path(project_output_dir) / 'construction' / 'manifest.yaml'
    if not manifest_path.is_file():
        return {}
    manifest = safe_load_yaml(manifest_path.read_text('utf-8'))
    return dict((manifest.get('current_graph') or {}).get('workflow_units') or {})


def build_unit_config(
    project_config: RunConfig,
    unit: WorkflowUnitConfig,
    *,
    unit_output_dir: str | Path,
) -> RunConfig:
    """Derive a scoped RunConfig for one parallel unit without mutating the parent."""
    cloned = project_config.model_copy(deep=True)
    if unit.source_ids:
        known = {s.id for s in cloned.sources}
        missing = sorted(set(unit.source_ids) - known)
        if missing:
            raise LightGraphError('source_not_found')
        cloned.sources = [s for s in cloned.sources if s.id in unit.source_ids]

    for sid, tables in unit.source_table_overrides.items():
        source = next((s for s in cloned.sources if s.id == sid), None)
        if source is None:
            raise LightGraphError('source_not_found')
        if source.allowed_tables and not set(tables) <= set(source.allowed_tables):
            raise LightGraphError('source_scope_missing')
        if not tables:
            raise LightGraphError('source_scope_missing')
        source.allowed_tables = list(tables)
        if source.allowed_columns is not None:
            source.allowed_columns = {
                tbl: cols for tbl, cols in source.allowed_columns.items() if tbl in set(tables)
            }

    active_tables_by_source = {
        s.id: set(s.allowed_tables) for s in cloned.sources if s.allowed_tables
    }
    active_source_ids = {s.id for s in cloned.sources}
    cloned.scope_bindings = [
        b for b in cloned.scope_bindings
        if b.source_id in active_source_ids and (
            b.source_id not in active_tables_by_source
            or f'{b.schema_name}.{b.table}' in active_tables_by_source[b.source_id]
            or b.table in active_tables_by_source[b.source_id]
        )
    ]

    if unit.document_ids is not None:
        wanted = set(unit.document_ids)
        cloned.documents = [
            doc for doc in cloned.documents
            if doc in wanted or Path(doc).name in wanted or Path(doc).stem in wanted
        ]

    base_goal = effective_goal_markdown(project_config)
    extra_parts = [base_goal] if base_goal.strip() else []
    extra_parts.append(f'## 并行单元上下文 ({unit.unit_id})\n\n仅处理当前单元已授权来源与表范围，产出的增量图将由项目融合器统一归并。')
    if unit.goal_markdown and unit.goal_markdown.strip():
        extra_parts.append(unit.goal_markdown.strip())
    if unit.probe_filter_hint and unit.probe_filter_hint.strip():
        extra_parts.append(f'### 探查范围建议\n\n{unit.probe_filter_hint.strip()}')
    cloned.goal_markdown = '\n\n'.join(extra_parts)

    if unit.max_run_seconds is not None:
        cloned.max_run_seconds = unit.max_run_seconds
    if unit.max_calls is not None:
        if cloned._agent is not None:
            cloned._agent = cloned._agent.model_copy(
                update={'llm': cloned._agent.llm.model_copy(update={'max_calls': unit.max_calls})}
            )
        else:
            cloned.model = cloned.model.model_copy(update={'max_calls': unit.max_calls})

    wf = project_config.workflow
    cloned.enable_co_projection = (
        unit.enable_co_projection
        if unit.enable_co_projection is not None
        else bool(wf.enable_co_projection) if wf is not None else project_config.enable_co_projection
    )
    cloned.workflow = None
    cloned.output_dir = str(unit_output_dir)
    return cloned


def copy_frozen_inheritance(parent_construction_dir: str | Path, unit_construction_dir: str | Path) -> None:
    """Propagate frozen read-only inherited graphs into a parallel unit workspace."""
    src_dir = Path(parent_construction_dir) / 'inherited'
    if not (src_dir / 'summary.yaml').is_file():
        return
    dst_dir = Path(unit_construction_dir) / 'inherited'
    dst_dir.mkdir(parents=True, exist_ok=True)
    summary_text = (src_dir / 'summary.yaml').read_text('utf-8')
    for ref in safe_load_yaml(summary_text)['inherited']:
        name = ref.get('snapshot_file') or (ref['inherit_id'] + '.yaml')
        if Path(name).name != name:
            raise LightGraphError('invalid_inherited_graph_snapshot')
        atomic_text(dst_dir / name, (src_dir / name).read_text('utf-8'))
    atomic_text(dst_dir / 'summary.yaml', summary_text)


def _read_workspace_evidence_map(construction_dir: Path) -> dict[str, dict[str, Any]]:
    manifest_path = construction_dir / 'manifest.yaml'
    if not manifest_path.is_file():
        return {}
    manifest = safe_load_yaml(manifest_path.read_text(encoding='utf-8'))
    records: dict[str, dict[str, Any]] = {}
    for ref, rel in (manifest.get('evidence') or {}).items():
        path = (construction_dir / rel).resolve()
        if path.is_relative_to(construction_dir.resolve()) and path.is_file():
            try:
                records[ref] = safe_load_yaml(path.read_text(encoding='utf-8'))
            except Exception:
                continue
    return records


def load_unit_contributor(unit_output_dir: str | Path, unit_id: str) -> dict[str, Any] | None:
    construction_dir = Path(unit_output_dir) / 'construction'
    manifest_path = construction_dir / 'manifest.yaml'
    if not manifest_path.is_file():
        return None
    manifest = safe_load_yaml(manifest_path.read_text(encoding='utf-8'))
    head = manifest.get('current_graph') if isinstance(manifest, dict) else None
    if not head or not head.get('path'):
        return None
    ws = GraphWorkspace(construction_dir, head['context_fingerprint'])
    graph = ws.read_published_graph()
    return {
        'contributor_id': unit_id,
        'kind': 'unit',
        'graph': graph,
        'evidence_records': _read_workspace_evidence_map(construction_dir),
    }


def merge_project_workflow(
    project_output_dir: str | Path,
    project_config: RunConfig,
    *,
    workspace=None,
    unit_ids: list[str] | None = None,
    unit_attempts: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Deterministically merge inherited base graphs and parallel workflow unit publications."""
    root_output = Path(project_output_dir)
    root_construction = root_output / 'construction'
    root_construction.mkdir(parents=True, exist_ok=True)

    contributors: list[dict[str, Any]] = []
    inherited_dir = root_construction / 'inherited'
    if getattr(project_config, 'inherited_graphs', None) and not (inherited_dir / 'summary.yaml').is_file():
        freeze_inherited_graphs(project_config, root_construction, workspace=workspace)

    contributors, _ = frozen_contributors(root_construction, config=project_config)
    wf = project_config.workflow
    units = {u.unit_id: u for u in wf.units} if wf else {}
    selected_units = list(units) if unit_ids is None else unit_ids
    attempts = published_unit_attempts(root_output) if unit_attempts is None else unit_attempts
    included_units: list[str] = []
    skipped_units: list[str] = []
    for uid in sorted(selected_units):
        if uid not in units:
            raise LightGraphError('workflow_unit_not_found')
        if uid not in attempts and unit_attempts is not None:
            skipped_units.append(uid)
            continue
        output_dir = unit_output_path(root_output, uid, attempts.get(uid))
        contrib = load_unit_contributor(output_dir, uid)
        if contrib is None:
            skipped_units.append(uid)
            continue
        expected_access = access_fingerprint(build_unit_config(project_config, units[uid], unit_output_dir=output_dir))
        if contrib['graph'].get('access_fingerprint') != expected_access:
            raise LightGraphError('graph_access_context_changed')
        contributors.append(contrib)
        included_units.append(uid)

    policy = wf.merge_policy if wf is not None else None
    merged = merge_construction_graphs(
        contributors,
        target_access_fingerprint=access_fingerprint(project_config),
        merge_policy=policy,
    )
    ctx_fingerprint = digest('workflow_merge_v2', access_fingerprint(project_config), merged)
    persisted = write_sharded_graph(root_construction, merged, context_fingerprint=ctx_fingerprint,
                                    unit_attempts={uid: attempts[uid] for uid in included_units if uid in attempts})
    merge_summary = {
        'format_version': 1,
        'merged_at': _now(),
        'included_units': included_units,
        'skipped_units': skipped_units,
        'inherited_ids': [c['contributor_id'] for c in contributors if c['kind'] == 'base'],
        'nodes': persisted['nodes'],
        'edges': persisted['edges'],
        'shard_count': persisted['shard_count'],
        'candidate_identity_merges': len(merged.get('candidate_identity_merges', [])),
        'candidate_column_alignments': len(merged.get('candidate_column_alignments', [])),
        'conflict_isolation': {
            k: v for k, v in (merged.get('conflict_isolation') or {}).items()
            if k not in {'node_ids', 'edge_ids'}
        },
    }
    write_yaml(root_construction / 'workflow-merge-summary.yaml', merge_summary)
    write_yaml(root_construction / 'merge-alignment-candidates.yaml', {
        'candidate_identity_merges': merged.get('candidate_identity_merges', []),
        'candidate_column_alignments': merged.get('candidate_column_alignments', []),
    })
    return merge_summary


def workflow_state_path(project_output_dir: str | Path) -> Path:
    return Path(project_output_dir) / 'workflow-state.json'


def save_workflow_state(project_output_dir: str | Path, state: dict[str, Any]) -> None:
    atomic_text(workflow_state_path(project_output_dir), json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2))


def load_workflow_state(project_output_dir: str | Path) -> dict[str, Any] | None:
    path = workflow_state_path(project_output_dir)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
