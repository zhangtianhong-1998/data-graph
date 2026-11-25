export type BusinessContext = Record<string, string | number | boolean>
export type GuidanceStage = 'T0_discovery' | 'T1_table_discovery' | 'T2_cross_table' | 'T3_hierarchy' | 'T4_document' | 'T5_revision' | 'semantic_review'
export type RuntimeGuidance = { text: string; injection: 'global' | 'task' | 'iteration' | 'semantic_review'; stages: GuidanceStage[] }
export interface DocumentScope { source_ids: string[]; tables: string[]; columns: string[] }
export interface ProjectFile {
  file_id: string; name: string; kind: 'excel' | 'csv' | 'markdown' | 'yaml'
  relative_path: string; size: number; purpose: string; scope: DocumentScope
  inactive_reason?: string | null
  binding_origin?: 'filename' | 'explicit' | null
  manifest_path?: string | null
}
export interface Project { status?: string; active_run?: Run; project_id: string; name: string; created_at: string; updated_at: string; files: ProjectFile[] }
export interface SourceConfig { id: string; kind: string; [key: string]: unknown }
export interface InheritedGraphRef {
  inherit_id: string
  source_project_id?: string | null
  snapshot_path?: string | null
  allowed_scopes: string[]
  expected_graph_digest?: string | null
}
export interface WorkflowPosition {
  x: number
  y: number
}
export interface WorkflowUnitConfig {
  unit_id: string
  label?: string | null
  source_ids: string[]
  source_table_overrides: Record<string, string[]>
  document_ids: string[] | null
  goal_markdown?: string | null
  probe_filter_hint?: string | null
  max_calls?: number | null
  max_run_seconds?: number | null
  enable_co_projection?: boolean | null
  position?: WorkflowPosition | null
}
export interface EntityFusionRule {
  entity_type: string; canonical_type: string; identity_domain: string
  key_mapping: Record<string, string>; property_aliases?: Record<string, string>
  key_normalizers?: Record<string, string[]>; property_normalizers?: Record<string, string[]>
  source_bindings?: Array<{ source_id: string; schema: string; table: string; key_columns: Record<string, string> }>
}
export interface FusionPolicy {
  version: 1; label_conflict_policy: 'retain_variants' | 'strict' | 'quarantine'
  entity_rules: EntityFusionRule[]
}
export interface MergePolicyConfig {
  reviewed_key_pairs?: Array<{
    approval_ref: string
    entity_type: string
    projections: Array<{ contributor_id: string; graph_digest: string; source_id: string; schema: string;
      table: string; key_column: string; name_column: string }>
    observation_digests: string[]
  }>
  fusion_policy?: FusionPolicy | null
  shared_entity_types: string[]
  authority_sources: string[]
  conflict_mode: 'quarantine' | 'strict'
  emit_alignment_candidates: boolean
}
export interface WorkflowMergeNodeConfig {
  node_id: string
  label?: string | null
  merge_mode: 'deterministic' | 'overlap_review' | 'merge_and_extract'
  source_ids: string[]
  source_table_overrides: Record<string, string[]>
  document_ids: string[] | null
  goal_markdown?: string | null
  probe_filter_hint?: string | null
  max_calls?: number | null
  max_run_seconds?: number | null
  enable_co_projection?: boolean | null
  merge_policy?: MergePolicyConfig | null
  position?: WorkflowPosition | null
}
export interface WorkflowEdgeConfig {
  edge_id: string
  source: string
  target: string
}
export interface WorkflowConfig {
  legacy_merge?: boolean
  enabled: boolean
  max_parallel_workers: number
  enable_co_projection: boolean
  units: WorkflowUnitConfig[]
  merge_nodes?: WorkflowMergeNodeConfig[]
  edges?: WorkflowEdgeConfig[]
  output_position?: WorkflowPosition | null
  merge_policy?: MergePolicyConfig
}
export interface WorkflowFailureDetail {
  stage: string
  exception_type: string
  attempts: number
  errno?: number
  winerror?: number
}
export interface WorkflowMergeCounts {
  input_nodes?: number
  output_nodes?: number
  input_edges?: number
  output_edges?: number
  output_directed_endpoint_pairs?: number
  output_multi_edge_pairs?: number
  output_endpoint_pair_excess?: number
  output_self_loop_edges?: number
  source_replay_status?: string
  source_replay_collapsed_nodes?: number
  observed_dictionary_collapsed_nodes?: number
  hierarchy_replay_collapsed_nodes?: number
}
export interface WorkflowMergeNodeState {
  node_id: string
  label?: string
  merge_mode?: string
  status: string
  run_id?: string | null
  error_code?: string | null
  failure_detail?: WorkflowFailureDetail
  merge_counts?: WorkflowMergeCounts
  delta_merge_counts?: WorkflowMergeCounts
  overlap_review?: {
    status: string
    reason?: string | null
    pattern_count: number
    eligible_patterns?: number
    proposed_patterns?: number
    verified_proposal_groups?: number
    applied_proposal_groups?: number
    proposal_collapsed_nodes?: number
    proposed_relation_patterns?: number
    verified_relation_facts?: number
    grouped_relation_edges?: number
    submitted_patterns?: number
    failed_patterns?: number
    analysis_omitted_patterns?: number
    reviewed_patterns: number
    omitted_patterns: number
    truncated?: boolean
    selected_patterns: number
    added_entity_rules: number
    model_calls: number
  }
  nodes?: number
  model_calls?: number | null
  edges?: number
  updated_at?: string
}
export interface RetrievalStatusResponse {
  status: 'no_graph' | 'not_vectorized' | 'stale' | 'vectorized' | 'indexed'
  indexed?: boolean
  vectorized: boolean
  stale_reason: string | null
  has_graph: boolean
  graph_nodes: number
  graph_edges: number
  configured_provider: string
  configured_model: string
  configured_base_url: string
  vector_dim: number | null
  vector_cards: number | null
  nodes_indexed: number | null
  edges_indexed: number | null
  embedder_model: string | null
  embedder_provider: string | null
  synced_at: string | null
  generation: string
  config_error: string | null
}
export interface EmbeddingConfig {
  provider?: 'none' | 'openai_compatible'
  base_url?: string
  model?: string
  api_key_env?: string
  allow_insecure_http?: boolean
  timeout_seconds?: number
  batch_size?: number
  dimensions?: number | null
  query_instruction?: string
}
export interface WorkflowUnitState {
  unit_id: string
  label?: string
  status: string
  run_id?: string | null
  error_code?: string | null
  failure_detail?: WorkflowFailureDetail
  nodes?: number
  model_calls?: number | null
  edges?: number
  updated_at?: string
}
export interface WorkflowState {
  recomposed_nodes?: string[]
  artifacts?: Record<string, { artifact_id: string; graph_kind: string; coverage?: string }>
  terminal_ids?: string[]
  resumed_from_run_id?: string
  parent_run_id?: string
  workflow_input?: string
  plan_mode?: string
  status?: string
  created_at?: string
  finished_at?: string | null
  units?: Record<string, WorkflowUnitState>
  merge_nodes?: Record<string, WorkflowMergeNodeState>
  merge?: Record<string, unknown>
}
export interface RunConfig {
  fusion_policy?: FusionPolicy
  max_run_seconds?: number
  max_db_queries?: number | null
  max_returned_rows?: number | null
  max_returned_bytes?: number | null
  max_working_bytes?: number
  construction_target?: 'instances' | 'structure'
  goal_markdown?: string | null
  goal?: { runtime_guidance?: RuntimeGuidance[]; purpose?: string; entity_kinds?: string[]; relation_kinds?: string[]; include?: string[]; exclude?: string[]; discovery_guidance?: string[]; [key: string]: unknown }
  sources?: SourceConfig[]
  scopes?: string[]
  documents?: string[]
  inherited_graphs?: InheritedGraphRef[]
  workflow?: WorkflowConfig | null
  embedding?: EmbeddingConfig
  model?: { mode?: string; base_url?: string; model?: string; api_key_env?: string; stream?: boolean; [key: string]: unknown }
  [key: string]: unknown
}
export interface Table { source_id: string; schema: string; name: string; columns: Array<{ name: string; data_type?: string; comment?: string; [key: string]: unknown }> }
export interface GraphNode { id: string; label?: string; name?: string; kind?: string; layer?: string; status?: string; [key: string]: unknown }
export interface StandardNodeAttributeOrigin { source_id: string; qualified_table: string; column: string | null; data_type: string }
export interface FactContext { version?: number; status: 'global' | 'scoped' | 'unknown'; dimensions: BusinessContext }
export interface AvailableRetrievalContext {
  status: 'partial' | 'source_text_only' | 'no_confirmed_match' | 'scope_unresolved' | 'search_unavailable'
  nodes: Array<{id: string; display_name?: string}>
  edges: Array<{id: string; source: string; target: string; relation: string}>
  witnesses: Array<{fact_id: string; row_id: string; conditions: Array<{text: string; node_id: string}>;
    source?: {source_id?: string; schema?: string; table?: string}}>
  passages: Array<{text: string; node_id: string; display_name?: string; excerpt: string;
    observation_id?: string | null; evidence_refs: string[]; source_tables: string[]}>
  candidate_path?: {
    assumptions: Array<{text: string; node_id: string; display_name: string; kind: string}>
    unverified_terms: string[]
    nodes: AvailableRetrievalContext['nodes']
    edges: AvailableRetrievalContext['edges']
    witnesses: AvailableRetrievalContext['witnesses']
    requested_level: string | null
    level_node_ids: string[]
  }
  truncated?: boolean
}
export interface AttributeObservation { binding_role?: 'attribute_column' | 'dedicated_column' | 'mapping_constant' | 'unlocated'; business_context?: FactContext; row_context?: { values?: Record<string, unknown> } | null; value: unknown; raw_value?: unknown; original_property?: string; source_id: string; qualified_table: string; column?: string | null; scope_ref?: string; evidence_refs?: string[]; observation_id?: string }
export interface StandardNodeAttribute { observations?: AttributeObservation[]; key: string; label: string; values: unknown[]; data_type: string; origin_tables: StandardNodeAttributeOrigin[] }
export interface StandardNodeSourceEvidenceField { role: string; field: string; column: string; data_type: string }
export interface StandardNodeSourceEvidence {
  source_id: string; source_kind: string; source_kind_label: string; schema: string; table: string; qualified_table: string
  key_columns: string[]; contributed_fields: StandardNodeSourceEvidenceField[]; scopes: string[]; evidence_refs: string[]
  inherited_from?: { inherit_id?: string; source_project_id?: string; source_graph_digest?: string } | null
}
export interface StandardNodeCatalogNote {
  source_id: string; qualified_table: string; table_comment: string
  column_comments: Array<{ column: string; data_type: string; comment: string }>; note_kind: string
}
export interface StandardNode {
  label_variants?: string[]; label_conflict?: boolean
  source_name?: string | null; source_names?: string[]; preferred_source_name?: string | null; type_family?: string
  id: string; kind: string; status: string; chinese_name: string | null; english_name: string | null
  english_name_status: 'mapped' | 'unknown'; display_name: string; aliases: string[]
  identity: Record<string, unknown>; identity_types: Record<string, string>
  business_description: string | null; business_description_origin: 'source_column' | 'entity_definition' | 'none'
  catalog_metadata_notes: StandardNodeCatalogNote[]; attributes: StandardNodeAttribute[]; source_evidences: StandardNodeSourceEvidence[]
  scopes: string[]
}
export interface GraphEdge { id?: string; status?: string; source: string; target: string; relation?: string; [key: string]: unknown }
export interface Graph { nodes: GraphNode[]; edges: GraphEdge[]; claims?: unknown[]; display_schema?: import('./graphLabels').DisplaySchema; [key: string]: unknown }
export interface Metadata extends Graph { tables: Table[]; files?: Array<{ name: string; content: string }>; using_saved_metadata?: boolean; refresh_failed?: boolean; requires_refresh?: boolean; metadata_cache_warning?: string | null }
export interface RunEvent {
  seq: number; time: string; stage: string; code: string; level: 'info' | 'warning' | 'error'
  table?: string; rule_id?: string; completed?: number; total?: number; unit?: string
  counters?: Record<string, number>
}
export interface RunProgressSnapshot {
  stage: string; completed: number | null; total: number | null; unit: string | null
  updated_at: string; elapsed_seconds: number; counters: Record<string, number>
  events: RunEvent[]; dropped_events: number
}
export interface Run {
  run_id: string; project_id: string; status: 'queued' | 'running' | 'completed' | 'completed_with_gaps' | 'partial' | 'failed' | 'cancelled' | 'interrupted'
  stage: string; created_at: string; finished_at?: string; report?: unknown; error_code?: string
  config_revision?: number
  run_mode?: 'continue' | 'fresh'
  workflow?: boolean
  workflow_units?: string[]
  current_workflow_run_id?: string | null
  workflow_state?: WorkflowState | null
  failure_detail?: WorkflowFailureDetail
  output_path?: string
  progress?: RunProgressSnapshot | null
  cleanup_status: 'pending' | 'completed' | 'unknown'
}
