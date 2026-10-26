export interface DocumentScope { source_ids: string[]; tables: string[]; columns: string[] }
export interface ProjectFile {
  file_id: string; name: string; kind: 'excel' | 'csv' | 'markdown' | 'yaml'
  relative_path: string; size: number; purpose: string; scope: DocumentScope
}
export interface Project { project_id: string; name: string; created_at: string; updated_at: string; files: ProjectFile[] }
export interface SourceConfig { id: string; kind: string; [key: string]: unknown }
export interface RunConfig {
  goal?: { purpose?: string; entity_kinds?: string[]; relation_kinds?: string[]; include?: string[]; exclude?: string[]; discovery_guidance?: string[]; [key: string]: unknown }
  sources?: SourceConfig[]
  model?: { mode?: string; base_url?: string; model?: string; api_key_env?: string; stream?: boolean; [key: string]: unknown }
  [key: string]: unknown
}
export interface Table { source_id: string; schema: string; name: string; columns: Array<{ name: string; data_type?: string; comment?: string; [key: string]: unknown }> }
export interface GraphNode { id: string; label?: string; name?: string; kind?: string; layer?: string; status?: string; [key: string]: unknown }
export interface GraphEdge { id?: string; status?: string; source: string; target: string; relation?: string; [key: string]: unknown }
export interface Graph { nodes: GraphNode[]; edges: GraphEdge[]; claims?: unknown[]; [key: string]: unknown }
export interface Metadata extends Graph { tables: Table[]; files?: Array<{ name: string; content: string }> }
export interface Run {
  run_id: string; project_id: string; status: 'queued' | 'running' | 'completed' | 'partial' | 'failed' | 'cancelled' | 'interrupted'
  stage: string; created_at: string; finished_at?: string; report?: unknown; error_code?: string
  config_revision?: number
  cleanup_status: 'pending' | 'completed' | 'unknown'
}
