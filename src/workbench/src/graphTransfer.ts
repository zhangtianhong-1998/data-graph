import { ApiError, projectPath, request } from './api'
import { mergeDisplaySchema } from './graphLabels'
import { edgeKey } from './graphScene'
import type { Graph, GraphEdge, GraphNode } from './types'

type Batch = Graph & { part: 'nodes' | 'edges'; offset: number; next_offset: number | null; generation: string; matched_nodes: number; matched_edges: number }
export type GraphTransfer = { part: 'nodes' | 'edges'; offset: number; generation: string; complete: boolean; nodes: Map<string, GraphNode>; edges: Map<string, GraphEdge>; graph: Graph }
export function newGraphTransfer(): GraphTransfer {
  return { part: 'nodes', offset: 0, generation: '', complete: false, nodes: new Map(), edges: new Map(), graph: { nodes: [], edges: [], truncated: true } }
}

/** A cursor advances only after an accepted page. Pausing keeps that cursor;
 * resuming rechecks its publication and authorization on the server. */
export async function transferGraph(projectId: string, selection: { layer: string; includeCandidates?: boolean; focus?: string; hops?: number; traversal?: string }, state: GraphTransfer, signal: AbortSignal, onPage: (graph: Graph) => void) {
  while (!state.complete) {
    if (signal.aborted) throw new ApiError('request_cancelled')
    const params = new URLSearchParams({ layer: selection.layer, include_candidates: String(selection.includeCandidates === true), part: state.part, offset: String(state.offset), limit: '1000', generation: state.generation, focus: selection.focus || '', hops: String(selection.hops ?? 2), traversal: selection.traversal ?? 'chains' })
    const page = await request<Batch>(`${projectPath(projectId)}/graph/batches?${params}`, { signal })
    if (signal.aborted) throw new ApiError('request_cancelled')
    if (state.generation && page.generation !== state.generation) throw new ApiError('graph_generation_changed')
    if (!page.generation || page.part !== state.part || page.offset !== state.offset || (page.next_offset !== null && (!Number.isInteger(page.next_offset) || page.next_offset <= state.offset))) throw new ApiError('graph_batch_invalid')
    state.generation = page.generation
    for (const node of page.nodes) state.nodes.set(node.id, node)
    for (const edge of page.edges) {
      if (!state.nodes.has(edge.source) || !state.nodes.has(edge.target)) throw new ApiError('graph_batch_invalid')
      state.edges.set(edgeKey(edge), edge)
    }
    if (page.next_offset !== null) state.offset = page.next_offset
    else if (state.part === 'nodes' && page.matched_edges > 0) { state.part = 'edges'; state.offset = 0 }
    else state.complete = true
    if (state.complete && (state.nodes.size !== page.matched_nodes || state.edges.size !== page.matched_edges)) {
      state.complete = false
      throw new ApiError('graph_batch_incomplete')
    }
    state.graph = { ...page, display_schema: mergeDisplaySchema(state.graph.display_schema, page.display_schema), nodes: [...state.nodes.values()], edges: [...state.edges.values()], next_offset: null, truncated: !state.complete, edges_truncated: false, neighborhood_truncated: !!selection.focus && !state.complete, fully_loaded: state.complete }
    onPage(state.graph)
  }
  return state.graph
}

export function invalidatesGraph(error: unknown): boolean {
  return error instanceof ApiError && ['graph_generation_changed', 'graph_read_conflict', 'graph_access_context_changed', 'project_not_found', 'session_invalid', 'session_required'].includes(error.code)
}
