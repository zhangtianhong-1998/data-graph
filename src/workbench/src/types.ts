export type GuidanceStage = 'T0_discovery' | 'T1_table_discovery' | 'T2_cross_table' | 'T3_hierarchy' | 'T4_document' | 'T5_revision' | 'semantic_review'
export type RuntimeGuidance = { text: string; injection: 'global' | 'task' | 'iteration' | 'semantic_review'; stages: GuidanceStage[] }
export interface DocumentScope { source_ids: string[]; tables: string[]; columns: string[] }
export interface ProjectFile {
  file_id: string; name: string; kind: 'excel' | 'csv' | 'markdown' | 'yaml'
  relative_path: string; size: number; purpose: string; scope: DocumentScope
}
export interface Project { project_id: string; name: string; created_at: string; updated_at: string; files: ProjectFile[] }
export interface SourceConfig { id: string; kind: string; [key: string]: unknown }
export interface RunConfig {
  max_run_seconds?: number
  max_db_queries?: number | null
  max_returned_rows?: number | null
  max_returned_bytes?: number | null
  max_working_bytes?: number
  construction_target?: 'instances' | 'structure'
  goal_markdown?: string | null
  goal?: { runtime_guidance?: RuntimeGuidance[]; purpose?: string; entity_kinds?: string[]; relation_kinds?: string[]; include?: string[]; exclude?: string[]; discovery_guidance?: string[]; [key: string]: unknown }
  sources?: SourceConfig[]
  model?: { mode?: string; base_url?: string; model?: string; api_key_env?: string; stream?: boolean; [key: string]: unknown }
  [key: string]: unknown
}
export interface Table { source_id: string; schema: string; name: string; columns: Array<{ name: string; data_type?: string; comment?: string; [key: string]: unknown }> }
export interface GraphNode { id: string; label?: string; name?: string; kind?: string; layer?: string; status?: string; [key: string]: unknown }
export interface StandardNodeAttributeOrigin { source_id: string; qualified_table: string; column: string | null; data_type: string }
export interface StandardNodeAttribute { key: string; label: string; values: unknown[]; data_type: string; origin_tables: StandardNodeAttributeOrigin[] }
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
  output_path?: string
  progress?: RunProgressSnapshot | null
  cleanup_status: 'pending' | 'completed' | 'unknown'
}
