"""In-project parallel workflow DAG planner, unit builder, merge orchestrator, and state persistence."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import uuid
from pathlib import Path
from typing import Any

from lightgraph.contracts import digest
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import (
    InheritedGraphRef,
    MergePolicyConfig,
    RunConfig,
    WorkflowConfig,
    WorkflowMergeNodeConfig,
    WorkflowUnitConfig,
)
from lightgraph.runtime.retention import atomic_text
from .access import access_fingerprint
from .artifact_io import artifact_step
from .graph_storage import _graph_digest
from .inheritance import freeze_inherited_graphs, frozen_contributors
from .intent import effective_goal_markdown
from .merger import identity_candidate_node_counts, merge_construction_graphs, write_sharded_graph
from .paths import io_path
from .workspace import GraphWorkspace, read_internal_yaml, write_yaml


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


def run_output_path(project, relative):
    """Resolve only project-owned output lineages from a persisted run record."""
    if not isinstance(relative, str) or not re.fullmatch(r'output(?:/generations/run_[0-9a-f]{24})?', relative):
        raise LightGraphError('invalid_output_generation')
    project = Path(project)
    path = project
    for part in Path(relative).parts:
        path = path / part
        if io_path(path).is_symlink():
            raise LightGraphError('invalid_output_generation')
    return path


def committed_unit_output(output, uid, attempt):
    """Follow an attempt to its original generation after an explicit resume."""
    local = unit_output_path(output, uid, attempt)
    output = Path(output)
    project = output.parent if output.name == 'output' else output.parents[2] if output.parent.name == 'generations' else None
    if attempt is None or project is None:
        return local
    run_file = io_path(project / 'runs' / (attempt + '.json'))
    if not run_file.exists():
        return local
    if run_file.is_symlink():
        raise LightGraphError('invalid_workflow_attempt')
    record = json.loads(run_file.read_text('utf-8'))
    if record.get('run_id') != attempt or record.get('project_id') != project.name:
        raise LightGraphError('invalid_workflow_attempt')
    return unit_output_path(run_output_path(project, record['output_path']), uid, attempt)


def published_unit_attempts(project_output_dir):
    manifest_path = io_path(Path(project_output_dir) / 'construction' / 'manifest.yaml')
    if not manifest_path.is_file():
        return {}
    manifest = read_internal_yaml(manifest_path)
    return dict((manifest.get('current_graph') or {}).get('workflow_units') or {})


_WORKSPACE_DOC_NAME = re.compile(r'^(f_[a-f0-9]{16})_(.+)$')


def _document_matches(doc: str, wanted: set[str]) -> bool:
    path = Path(doc)
    name = path.name
    stem = path.stem
    norm = doc.replace('\\', '/')
    match = _WORKSPACE_DOC_NAME.match(name)
    file_id = match.group(1) if match else None
    orig_name = match.group(2) if match else None
    orig_stem = Path(orig_name).stem if orig_name else None
    for token in wanted:
        w = str(token).strip()
        if not w:
            continue
        w_norm = w.replace('\\', '/')
        if doc == w or name == w or stem == w or norm.endswith('/' + w_norm):
            return True
        if file_id and w == file_id:
            return True
        if orig_name and (w == orig_name or w == orig_stem):
            return True
    return False


@dataclass(frozen=True)
class WorkflowPlan:
    mode: str  # 'direct_single' | 'direct_multi' | 'dag_merge' | 'legacy_merge'
    extract_ids: list[str]
    merge_order: list[str]
    merge_inputs: dict[str, list[str]]
    terminal_ids: list[str]
    explicit_dag: bool


def resolve_workflow_plan(wf: WorkflowConfig) -> WorkflowPlan:
    """Resolve an orchestrated DAG (extract nodes, optional merge nodes, optional output wires)."""
    unit_ids = [u.unit_id for u in wf.units]
    merge_ids = [m.node_id for m in wf.merge_nodes]
    unit_set = set(unit_ids)
    merge_set = set(merge_ids)
    explicit_dag = bool(wf.merge_nodes or wf.edges)

    if not explicit_dag:
        mode = ('direct_single' if len(unit_ids) == 1 else
                'legacy_merge' if wf.legacy_merge else 'direct_multi')
        return WorkflowPlan(
            mode=mode,
            extract_ids=list(unit_ids),
            merge_order=[],
            merge_inputs={},
            terminal_ids=list(unit_ids),
            explicit_dag=False,
        )

    outgoing: dict[str, list[str]] = {nid: [] for nid in unit_ids + merge_ids}
    incoming: dict[str, list[str]] = {nid: [] for nid in merge_ids + ['output']}
    for edge in wf.edges:
        outgoing.setdefault(edge.source, []).append(edge.target)
        incoming.setdefault(edge.target, []).append(edge.source)

    # Determine terminal nodes feeding final output.
    if incoming['output']:
        terminal_ids = list(dict.fromkeys(incoming['output']))
    else:
        terminal_ids = [node_id for node_id in unit_ids + merge_ids if not outgoing[node_id]]

    effective_incoming: dict[str, list[str]] = {
        mid: list(dict.fromkeys(incoming.get(mid, [])))
        for mid in merge_ids
    }

    # Backward traversal from terminal nodes to collect active extract & merge nodes.
    reachable_extract: set[str] = set()
    reachable_merge: set[str] = set()
    stack = list(terminal_ids)
    visited: set[str] = set()
    while stack:
        cur = stack.pop()
        if cur in visited:
            continue
        visited.add(cur)
        if cur in unit_set:
            reachable_extract.add(cur)
        elif cur in merge_set:
            reachable_merge.add(cur)
            for src in effective_incoming.get(cur, []):
                stack.append(src)

    # Preserve declared order for active extract nodes; topological order for active merge nodes.
    extract_ids = [uid for uid in unit_ids if uid in reachable_extract]
    merge_order: list[str] = []
    placed = set(extract_ids)
    remaining = [mid for mid in merge_ids if mid in reachable_merge]
    while remaining:
        progressed = False
        for mid in list(remaining):
            deps = [d for d in effective_incoming.get(mid, []) if d in reachable_extract or d in reachable_merge]
            if all(d in placed for d in deps):
                merge_order.append(mid)
                placed.add(mid)
                remaining.remove(mid)
                progressed = True
        if not progressed:
            raise LightGraphError('workflow_cycle_detected')

    if merge_order:
        mode = 'dag_merge'
    elif len(terminal_ids) <= 1:
        mode = 'direct_single'
    else:
        mode = 'direct_multi'

    return WorkflowPlan(
        mode=mode,
        extract_ids=extract_ids,
        merge_order=merge_order,
        merge_inputs={mid: [d for d in effective_incoming.get(mid, []) if d in placed] for mid in merge_order},
        terminal_ids=terminal_ids,
        explicit_dag=True,
    )


def _apply_scoped_inputs(
    cloned: RunConfig,
    *,
    source_ids: list[str],
    source_table_overrides: dict[str, list[str]],
    document_ids: list[str] | None,
) -> RunConfig:
    project_sources = cloned.sources
    if document_ids is not None:
        wanted = set(document_ids)
        cloned.documents = [doc for doc in cloned.documents if _document_matches(doc, wanted)]
    narrowed = (source_ids and set(source_ids) != {s.id for s in project_sources}) or source_table_overrides
    if narrowed and cloned.documents:
        from lightgraph.documents.core import resolve_workflow_table_selectors
        resolved = resolve_workflow_table_selectors(
            (d for d in cloned.documents if Path(d).is_file()), project_sources, cloned.removed_document_ids)
    else:
        resolved = {}
    if source_ids:
        known = {s.id for s in cloned.sources}
        missing = sorted(set(source_ids) - known)
        if missing:
            raise LightGraphError('source_not_found')
        cloned.sources = [s for s in cloned.sources if s.id in source_ids]

    for sid, tables in source_table_overrides.items():
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
    def bound_in_unit(address):
        for sid in active_source_ids:
            prefix = sid + ':'
            if address.startswith(prefix):
                table = address[len(prefix):]
                allowed = active_tables_by_source.get(sid)
                if allowed is None or table in allowed or table.split('.', 1)[-1] in allowed:
                    return True
        return False

    cloned._document_table_bindings = {key: table for key, table in resolved.items() if bound_in_unit(table)}
    cloned.scope_bindings = [
        b for b in cloned.scope_bindings
        if b.source_id in active_source_ids and (
            b.source_id not in active_tables_by_source
            or f'{b.schema_name}.{b.table}' in active_tables_by_source[b.source_id]
            or b.table in active_tables_by_source[b.source_id]
        )
    ]

    return cloned


def _shared_unit_tables(project_config: RunConfig, unit: WorkflowUnitConfig) -> list[str]:
    wf = project_config.workflow
    if not wf or len(wf.units) < 2:
        return []
    def scope_keys(item: WorkflowUnitConfig) -> set[tuple[str, str]]:
        keys: set[tuple[str, str]] = set()
        for sid in item.source_ids:
            tables = item.source_table_overrides.get(sid)
            if tables:
                keys.update((sid, t) for t in tables)
            else:
                keys.add((sid, '*'))
        return keys

    mine = scope_keys(unit)
    if not mine:
        return []
    shared: set[str] = set()
    for peer in wf.units:
        if peer.unit_id == unit.unit_id:
            continue
        other = scope_keys(peer)
        for sid, table in mine:
            if (sid, table) in other or (sid, '*') in other or (table == '*' and any(s == sid for s, _ in other)):
                shared.add(sid if table == '*' else f'{sid}.{table}')
    return sorted(shared)


def build_unit_config(
    project_config: RunConfig,
    unit: WorkflowUnitConfig,
    *,
    unit_output_dir: str | Path,
) -> RunConfig:
    """Derive a scoped RunConfig for one parallel extract node without mutating the parent."""
    cloned = project_config.model_copy(deep=True)
    _apply_scoped_inputs(
        cloned,
        source_ids=unit.source_ids,
        source_table_overrides=unit.source_table_overrides,
        document_ids=unit.document_ids,
    )

    base_goal = effective_goal_markdown(project_config)
    extra_parts = [base_goal] if base_goal.strip() else []
    unit_heading = f'{unit.label} ({unit.unit_id})' if unit.label else unit.unit_id
    shared_tables = _shared_unit_tables(project_config, unit)
    shared_note = (
        f'\n\n### 跨单元共享来源约定\n\n以下来源/表同时被其他并行单元读取：{", ".join(shared_tables)}。'
        '在这些共享来源上建模时，请直接以物理编码列的规范 `lower_snake_case` 命名 `key_names`，'
        '避免自定义别名键名，并确保同名实体保持一致的 `key_names`、`identity_scope` 与 `applicability`。'
        if shared_tables else ''
    )
    extra_parts.append(
        f'## 并行单元上下文 ({unit_heading})\n\n'
        '仅处理当前单元已授权来源与表范围，产出的增量图将由项目融合器统一归并。'
        '请保持跨单元契约一致性：逻辑键名统一使用规范的 `lower_snake_case`（如 `code` 或业务编码逻辑名），'
        '单列编码已能唯一标识实体时勿将名称列塞入复合键 `key_columns`（名称保留在 `label_column`）；'
        '如文档明确说明共享编码体系或适用范围，请规范填写 `identity_scope`、`identity_domain`、'
        '`identity_evidence_refs`、`identity_explanations` 与 `applicability`。'
        + shared_note
    )
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


def upstream_binding_inventory(graph: dict[str, Any]) -> str:
    """Summarize an authorized base by physical binding, without sending row values."""
    bindings = {}
    for node in graph.get('nodes', []):
        node_bindings = set()
        for support in node.get('supports', []):
            origin = support.get('provenance') or {}
            source = origin.get('source') or {}
            table, columns = source.get('table'), origin.get('key_columns')
            if table and isinstance(columns, list) and columns:
                node_bindings.add((str(node.get('type', '')), str(source.get('source_id', '')),
                                   str(source.get('schema') or ''), str(table), tuple(map(str, columns))))
        for key in node_bindings:
            bindings[key] = bindings.get(key, 0) + 1
    if not bindings:
        return ''
    rows = []
    size = 0
    for (kind, source, schema, table, cols), count in sorted(bindings.items(), key=lambda x: (-x[1], x[0])):
        row = f"- {kind} ← {'.'.join(filter(None, (source, schema, table)))} ({', '.join(cols)}) · {count} 个节点"
        if len(rows) >= 32 or size + len(row) > 3200:
            break
        rows.append(row)
        size += len(row) + 1
    if len(rows) < len(bindings):
        rows.append(f'另有 {len(bindings) - len(rows)} 项绑定未列出。')
    return '### 上游已投影的来源和实体键\n\n' + '\n'.join(rows)


def build_merge_node_config(
    project_config: RunConfig,
    merge_node: WorkflowMergeNodeConfig,
    *,
    upstream_snapshot_path: str | Path,
    upstream_summary: str,
    node_output_dir: str | Path,
) -> RunConfig:
    """Derive a RunConfig for a full-node merge-and-extract step with frozen upstream base."""
    cloned = project_config.model_copy(deep=True)
    _apply_scoped_inputs(
        cloned,
        source_ids=merge_node.source_ids,
        source_table_overrides=merge_node.source_table_overrides,
        document_ids=merge_node.document_ids,
    )

    inherit_id = f'upstream_{merge_node.node_id}'
    if len(inherit_id) > 64:
        inherit_id = f'upstream_{digest(merge_node.node_id)[:32]}'
    # Upstream is already the complete union of project inheritance and its
    # branches. Re-adding project references makes base identities ambiguous.
    cloned.inherited_graphs = [
        InheritedGraphRef(
            inherit_id=inherit_id,
            snapshot_path=str(Path(upstream_snapshot_path).resolve()),
            allowed_scopes=list(cloned.scopes or ['public']),
        ),
    ]

    base_goal = effective_goal_markdown(project_config)
    heading = f'{merge_node.label} ({merge_node.node_id})' if merge_node.label else merge_node.node_id
    extra_parts = [
        f'## 跨分支补充任务（{heading}）\n\n'
        f'上游已发布 {upstream_summary}。编排器会保留这些成果。'
        '先核对继承类型、实体键和已有关系，只提交上游缺少且有证据的关系、属性或实体。'
        '关系已有端点时，使用 `__base_graph__` 和继承类型的有序键查找端点。'
        '同一绑定的类型别名或另一份规则不构成新实体。已有结果覆盖任务时可以结束。'
    ]
    if base_goal.strip():
        extra_parts.append('### 项目背景（供核对已有覆盖范围）\n\n' + base_goal.strip())
    if merge_node.goal_markdown and merge_node.goal_markdown.strip():
        extra_parts.append(merge_node.goal_markdown.strip())
    if merge_node.probe_filter_hint and merge_node.probe_filter_hint.strip():
        extra_parts.append(f'### 全节点探查范围建议\n\n{merge_node.probe_filter_hint.strip()}')
    cloned.goal_markdown = '\n\n'.join(extra_parts)

    if merge_node.max_run_seconds is not None:
        cloned.max_run_seconds = merge_node.max_run_seconds
    if merge_node.max_calls is not None:
        if cloned._agent is not None:
            cloned._agent = cloned._agent.model_copy(
                update={'llm': cloned._agent.llm.model_copy(update={'max_calls': merge_node.max_calls})}
            )
        else:
            cloned.model = cloned.model.model_copy(update={'max_calls': merge_node.max_calls})

    wf = project_config.workflow
    cloned.fusion_policy = effective_merge_policy(project_config, merge_node).fusion_policy
    cloned.enable_co_projection = (
        merge_node.enable_co_projection
        if merge_node.enable_co_projection is not None
        else bool(wf.enable_co_projection) if wf is not None else project_config.enable_co_projection
    )
    cloned.workflow = None
    cloned.output_dir = str(node_output_dir)
    return cloned


def copy_frozen_inheritance(parent_construction_dir: str | Path, unit_construction_dir: str | Path) -> None:
    """Propagate frozen read-only inherited graphs into a parallel unit workspace."""
    src_dir = Path(parent_construction_dir) / 'inherited'
    if not (src_dir / 'summary.yaml').is_file():
        return
    dst_dir = Path(unit_construction_dir) / 'inherited'
    dst_dir.mkdir(parents=True, exist_ok=True)
    summary = read_internal_yaml(src_dir / 'summary.yaml')
    from .graph_storage import read_graph_record, write_graph_record
    for ref in summary['inherited']:
        name = ref.get('snapshot_file') or (ref['inherit_id'] + '.yaml')
        if Path(name).name != name or name in {'.', '..'}:
            raise LightGraphError('invalid_inherited_graph_snapshot')
        source = src_dir / name
        if source.is_symlink():
            raise LightGraphError('invalid_inherited_graph_snapshot')
        if source.is_dir():
            graph = read_graph_record(source)
            if ref.get('frozen_digest') and digest(graph) != ref['frozen_digest']:
                raise LightGraphError('inherited_graph_digest_mismatch')
            name = uuid.uuid4().hex + '.graph'
            write_graph_record(dst_dir / name, graph)
            ref['snapshot_file'] = name
        else:
            atomic_text(dst_dir / name, source.read_text('utf-8'))
    write_yaml(dst_dir / 'summary.yaml', summary)


def _read_workspace_evidence_map(construction_dir: Path) -> dict[str, dict[str, Any]]:
    manifest_path = construction_dir / 'manifest.yaml'
    if not manifest_path.is_file():
        return {}
    manifest = read_internal_yaml(manifest_path)
    records: dict[str, dict[str, Any]] = {}
    for ref, rel in (manifest.get('evidence') or {}).items():
        path = (construction_dir / rel).resolve()
        if path.is_relative_to(construction_dir.resolve()) and path.is_file():
            try:
                records[ref] = read_internal_yaml(path)
            except Exception:
                continue
    return records


def load_unit_contributor(unit_output_dir: str | Path, unit_id: str) -> dict[str, Any] | None:
    construction_dir = io_path(unit_output_dir) / 'construction'
    manifest_path = construction_dir / 'manifest.yaml'
    if not manifest_path.is_file():
        return None
    manifest = read_internal_yaml(manifest_path)
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


def publish_premerged_graph(
    project_output_dir: str | Path,
    project_config: RunConfig,
    *,
    merged: dict[str, Any],
    included_units: list[str],
    skipped_units: list[str],
    unit_attempts: dict[str, str],
    mode: str = 'dag_merge',
) -> dict[str, Any]:
    """Commit the Parquet graph, merge summary and candidate report with one manifest switch."""
    root_output = io_path(project_output_dir)
    root_construction = root_output / 'construction'
    root_construction.mkdir(parents=True, exist_ok=True)
    from .snapshot_registry import register_access_context
    policy_id = register_access_context(root_construction, project_config)
    ctx_fingerprint = _graph_digest(merged, 'workflow_merge_v2', access_fingerprint(project_config))
    merge_summary = {
        'format_version': 1,
        'mode': mode,
        'merged_at': _now(),
        'included_units': included_units,
        'skipped_units': skipped_units,
        'inherited_ids': sorted({
            sup['inherited_from']['inherit_id']
            for item in merged.get('nodes', []) + merged.get('edges', [])
            for sup in item.get('supports', [])
            if isinstance(sup.get('inherited_from'), dict) and sup['inherited_from'].get('inherit_id')
        }),
        'nodes': len(merged.get('nodes', [])),
        'edges': len(merged.get('edges', [])),
        'shard_count': 2,
        # A promoted checkpoint retains the producing pass. Historical artifacts
        # without counters cannot say which matching paths were executed.
        **({'merge_counts': merged['fusion_diagnostics']} if 'fusion_diagnostics' in merged else {}),
        'candidate_identity_merges': len(merged.get('candidate_identity_merges', [])),
        **identity_candidate_node_counts(merged),
        'candidate_column_alignments': len(merged.get('candidate_column_alignments', [])),
        'pending_definitions': len(merged.get('definition_conflicts', [])),
        'pending_relations': len(merged.get('unresolved_relations', [])),
        'pending_topology_checks': len(merged.get('context_cycle_diagnostics', [])),
        'semantic_status': 'pending_alignment' if any(merged.get(field) for field in (
            'definition_conflicts', 'unresolved_relations', 'context_cycle_diagnostics',
            'candidate_identity_merges', 'candidate_column_alignments')) else 'no_pending_alignment',
        'conflict_isolation': {
            k: v for k, v in (merged.get('conflict_isolation') or {}).items()
            if k not in {'node_ids', 'edge_ids'}
        },
    }

    def write_reports(directory):
        write_yaml(directory / 'workflow-merge-summary.yaml', merge_summary)
        write_yaml(directory / 'merge-alignment-candidates.yaml', {
            'candidate_identity_merges': merged.get('candidate_identity_merges', []),
            'candidate_column_alignments': merged.get('candidate_column_alignments', []),
            'definition_conflicts': merged.get('definition_conflicts', []),
            'unresolved_relations': merged.get('unresolved_relations', []),
            'context_cycle_diagnostics': merged.get('context_cycle_diagnostics', []),
        })

    write_sharded_graph(
        root_construction,
        merged,
        context_fingerprint=ctx_fingerprint,
        result_policy_id=policy_id,
        write_workflow_reports=write_reports,
        unit_attempts={uid: unit_attempts[uid] for uid in included_units if uid in unit_attempts},
    )
    return merge_summary


def published_workflow_summary(project_output_dir):
    root = io_path(project_output_dir) / 'construction'
    manifest_path = root / 'manifest.yaml'
    if not manifest_path.is_file():
        return None
    manifest = read_internal_yaml(manifest_path)
    snapshot = manifest.get('workflow_report_snapshot')
    if snapshot is not None:
        if not isinstance(snapshot, str) or not re.fullmatch(r'published/[a-f0-9]{32}\.graph', snapshot):
            raise LightGraphError('invalid_graph_workspace_record')
        directory = root / snapshot
        if directory.is_symlink() or directory.parent.is_symlink():
            raise LightGraphError('invalid_graph_workspace_record')
        summary = directory / 'workflow-merge-summary.yaml'
    else:
        summary = root / 'workflow-merge-summary.yaml'
    if summary.is_symlink():
        raise LightGraphError('invalid_graph_workspace_record')
    return read_internal_yaml(summary) if summary.is_file() else None


def effective_merge_policy(config, node=None):
    workflow_policy = config.workflow.merge_policy if config.workflow else MergePolicyConfig()
    policy = (getattr(node, 'merge_policy', None) or workflow_policy).model_copy(deep=True)
    inherited = policy.fusion_policy or workflow_policy.fusion_policy
    if inherited is None and policy.conflict_mode == "strict" and "fusion_policy" not in config.model_fields_set:
        inherited = config.fusion_policy.model_copy(update={"label_conflict_policy": "strict"})
    policy.fusion_policy = inherited or config.fusion_policy
    return policy


def deterministic_stage_inputs(input_ids, *, units, merged, merge_nodes, plan, artifacts, policy, config, bases):
    """Reuse original unit checkpoints through transparent deterministic stages in the same workflow.

    A new peer may contradict an earlier fold. Rechecking all leaves can undo the old
    fold as well as extend it. A worker delta, inherited publication, different policy
    or a partially isolated stage remains a self-contained input with its own decisions.
    """
    direct = [units[key] if key in units else merged[key] for key in input_ids]
    if not any(key in merged for key in input_ids):
        return [*bases, *direct]
    if bases:
        return direct
    leaves = {}
    pending = list(input_ids)
    visited = set()
    while pending:
        key = pending.pop()
        if key in visited:
            continue
        visited.add(key)
        record = artifacts.get(key, {})
        if key in units:
            if (not record.get('artifact_id') or record.get('status') != 'completed'
                    or record.get('coverage') == 'incomplete' or record.get('inherited_grants')
                    or units[key]['graph'].get('extends_base_graph')):
                return direct
            leaves[key] = units[key]
            continue
        node = merge_nodes.get(key)
        parents = plan.merge_inputs.get(key, [])
        if (node is None or node.merge_mode != 'deterministic' or not parents
                or effective_merge_policy(config, node) != policy
                or not record.get('artifact_id') or record.get('status') != 'completed' or record.get('coverage') != 'complete'
                or record.get('inherited_grants') or record.get('recomposed_from')
                or record.get('upstream_artifacts') != [artifacts.get(p, {}).get('artifact_id') for p in parents]):
            return direct
        pending.extend(parents)
    return [leaves[key] for key in sorted(leaves)]


def publish_merged_contributors(
    project_output_dir: str | Path,
    project_config: RunConfig,
    *,
    contributors: list[dict[str, Any]],
    included_units: list[str],
    skipped_units: list[str],
    unit_attempts: dict[str, str],
    merge_policy: MergePolicyConfig | None = None,
    mode: str = 'merged',
    independent_terminals: bool = False,
) -> dict[str, Any]:
    """Merge the verified contributors and publish the project graph."""
    if not contributors:
        raise LightGraphError('workflow_no_committed_contributors')
    # Independent outputs must not inherit a cross-unit identity merge policy.
    direct = independent_terminals or mode in {'direct_output', 'direct_multi_output'}
    policy = (MergePolicyConfig(emit_alignment_candidates=False)
              if direct else
              merge_policy or effective_merge_policy(project_config))
    merged = merge_construction_graphs(
        contributors,
        target_access_fingerprint=access_fingerprint(project_config),
        merge_policy=policy,
        independent_outputs=direct,
    )
    return publish_premerged_graph(
        project_output_dir,
        project_config,
        merged=merged,
        included_units=included_units,
        skipped_units=skipped_units,
        unit_attempts=unit_attempts,
        mode=mode,
    )


def merge_project_workflow(
    project_output_dir: str | Path,
    project_config: RunConfig,
    *,
    workspace=None,
    unit_ids: list[str] | None = None,
    unit_attempts: dict[str, str] | None = None,
    mode: str = 'merged',
    load_committed=None,
) -> dict[str, Any]:
    """Deterministically merge inherited base graphs and parallel workflow unit publications."""
    root_output = io_path(project_output_dir)
    root_construction = root_output / 'construction'
    root_construction.mkdir(parents=True, exist_ok=True)

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
        if uid not in attempts and load_committed is not None:
            historical = io_path(unit_output_path(root_output, uid) / 'construction' / 'manifest.yaml')
            if historical.is_file() and read_internal_yaml(historical).get('current_graph'):
                from .checkpoints import WorkflowCheckpointError
                raise WorkflowCheckpointError('workflow_checkpoint_missing', uid)
            skipped_units.append(uid)
            continue
        if uid not in attempts and unit_attempts is not None:
            skipped_units.append(uid)
            continue
        if uid in attempts and load_committed is not None:
            contrib = load_committed(units[uid], attempts[uid])
        else:
            output_dir = committed_unit_output(root_output, uid, attempts.get(uid))
            contrib = load_unit_contributor(output_dir, uid)
            if contrib is None:
                skipped_units.append(uid)
                continue
            scoped = build_unit_config(project_config, units[uid], unit_output_dir=output_dir)
            if contrib['graph'].get('access_fingerprint') != access_fingerprint(scoped):
                raise LightGraphError('graph_access_context_changed')
        contributors.append(contrib)
        included_units.append(uid)

    return publish_merged_contributors(
        root_output,
        project_config,
        contributors=contributors,
        included_units=included_units,
        skipped_units=skipped_units,
        unit_attempts=attempts,
        mode=mode,
    )


def workflow_state_path(project_output_dir: str | Path) -> Path:
    return Path(project_output_dir) / 'workflow-state.json'


def _state_path(output, run_id=None):
    if run_id is not None and (not isinstance(run_id, str) or not re.fullmatch(r'run_[a-f0-9]{24}', run_id)):
        raise LightGraphError('run_not_found')
    path = io_path(Path(output) / 'workflow-runs' / (run_id + '.json')
                   if run_id else workflow_state_path(output))
    if path.parent.is_symlink() or path.is_symlink():
        raise LightGraphError('workspace_path_invalid')
    return path


def archive_workflow_state(project_output_dir, state):
    """Keep the last pre-checkpoint installation's state before switching runs."""
    if state:
        path = _state_path(project_output_dir, state['parent_run_id'])
        if not path.exists():
            payload = json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2)
            artifact_step('state_history_write', atomic_text, path, payload)


def save_workflow_state(project_output_dir: str | Path, state: dict[str, Any], *, current_only=False) -> None:
    payload = json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2)
    rid = state.get('parent_run_id')
    if rid and not current_only:
        path = _state_path(project_output_dir, rid)
        artifact_step('state_history_write', atomic_text, path, payload)
    artifact_step('state_current_write', atomic_text, _state_path(project_output_dir), payload)


def interrupt_workflow_state(output, run_id, finished_at):
    """Close active stages in this run's archive without moving another run's head."""
    state = load_workflow_state(output, run_id)
    if not state or state.get('parent_run_id') != run_id or state.get('status') != 'running':
        return
    state.update(status='interrupted', error_code='service_restarted', finished_at=finished_at)
    nodes = [state.get('merge')]
    for key in ('units', 'merge_nodes'):
        bucket = state.get(key)
        if isinstance(bucket, dict):
            nodes.extend(bucket.values())
    for item in nodes:
        if isinstance(item, dict) and item.get('status') in ('queued', 'running', 'pending'):
            item.update(status='interrupted', error_code='service_restarted', updated_at=finished_at)
    payload = json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2)
    artifact_step('state_history_write', atomic_text, _state_path(output, run_id), payload)
    head = load_workflow_state(output)
    if head and head.get('parent_run_id') == run_id:
        artifact_step('state_current_write', atomic_text, _state_path(output), payload)


def load_workflow_state(project_output_dir: str | Path, run_id: str | None = None) -> dict[str, Any] | None:
    path = _state_path(project_output_dir, run_id)
    if run_id and not path.exists():
        # Old installations have only the last state, never borrow another run.
        current = load_workflow_state(project_output_dir)
        return current if current and current.get('parent_run_id') == run_id else None
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None
