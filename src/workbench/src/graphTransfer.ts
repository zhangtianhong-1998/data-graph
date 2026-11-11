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
    let page: Batch | undefined
    for (let attempt = 0; ; attempt++) {
      try { page = await request<Batch>(`${projectPath(projectId)}/graph/batches?${params}`, { signal }); break }
      catch (error) {
        // Only idempotent reads at the same accepted cursor may retry. A
        // changed publication/permission never uses this recovery path.
        if (signal.aborted || attempt >= 2 || !(error instanceof ApiError)
          || (!['network_unavailable', 'request_timeout', 'http_502', 'http_503', 'http_504'].includes(error.code)
            && ![502, 503, 504].includes(error.status))) throw error
        await new Promise<void>(resolve => {
          const done = () => { clearTimeout(timer); signal.removeEventListener('abort', done); resolve() }
          const timer = setTimeout(done, 100 * (attempt + 1))
          signal.addEventListener('abort', done, { once: true })
        })
        if (signal.aborted) throw new ApiError('request_cancelled')
      }
    }
    if (signal.aborted) throw new ApiError('request_cancelled')
    if (state.generation && page.generation !== state.generation) throw new ApiError('graph_generation_changed')
    if (!page.generation || page.part !== state.part || page.offset !== state.offset || (page.next_offset !== null && (!Number.isInteger(page.next_offset) || page.next_offset <= state.offset))) throw new ApiError('graph_batch_invalid')
    if (!Array.isArray(page.nodes) || !Array.isArray(page.edges)
      || !Number.isInteger(page.matched_nodes) || page.matched_nodes < 0
      || !Number.isInteger(page.matched_edges) || page.matched_edges < 0
      || (state.part === 'nodes' ? page.edges.length > 0 : page.nodes.length > 0)) throw new ApiError('graph_batch_invalid')
    const newNodes = new Map<string, GraphNode>(), newEdges = new Map<string, GraphEdge>()
    for (const node of page.nodes) {
      if (!node || typeof node.id !== 'string' || !node.id || state.nodes.has(node.id) || newNodes.has(node.id)) throw new ApiError('graph_batch_invalid')
      newNodes.set(node.id, node)
    }
    for (const edge of page.edges) {
      if (!edge || !state.nodes.has(edge.source) || !state.nodes.has(edge.target)) throw new ApiError('graph_batch_invalid')
      const key = edgeKey(edge)
      if (state.edges.has(key) || newEdges.has(key)) throw new ApiError('graph_batch_invalid')
      newEdges.set(key, edge)
    }
    const count = state.part === 'nodes' ? page.nodes.length : page.edges.length
    const total = state.part === 'nodes' ? page.matched_nodes : page.matched_edges
    const end = state.offset + count
    if (page.next_offset !== null && (page.next_offset !== end || end >= total)) throw new ApiError('graph_batch_invalid')
    if (page.next_offset === null && end !== total) throw new ApiError('graph_batch_incomplete')
    const complete = page.next_offset === null && (state.part === 'edges' || page.matched_edges === 0)
    if (complete && (state.nodes.size + newNodes.size !== page.matched_nodes || state.edges.size + newEdges.size !== page.matched_edges)) throw new ApiError('graph_batch_incomplete')
    // Commit only after every record and completion count has passed. A bad
    // page cannot contaminate the retained scene or move the resume cursor.
    state.generation = page.generation
    for (const [id, node] of newNodes) state.nodes.set(id, node)
    for (const [id, edge] of newEdges) state.edges.set(id, edge)
    if (page.next_offset !== null) state.offset = page.next_offset
    else if (state.part === 'nodes' && page.matched_edges > 0) { state.part = 'edges'; state.offset = 0 }
    state.complete = complete
    state.graph = { ...page, display_schema: mergeDisplaySchema(state.graph.display_schema, page.display_schema), nodes: [...state.nodes.values()], edges: [...state.edges.values()], next_offset: null, truncated: !state.complete, edges_truncated: false, neighborhood_truncated: !!selection.focus && !state.complete, fully_loaded: state.complete }
    onPage(state.graph)
  }
  return state.graph
}

export function invalidatesGraph(error: unknown): boolean {
  return error instanceof ApiError && ['graph_generation_changed', 'graph_read_conflict', 'graph_access_context_changed', 'project_not_found', 'session_invalid', 'session_required'].includes(error.code)
}
