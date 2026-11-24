import type { GraphEdge } from './types'

export function isHierarchyLoop(edge: GraphEdge) {
  return edge.source === edge.target && (edge.hierarchy_direction === 'outgoing' || edge.hierarchy_direction === 'incoming')
}

/** Each group describes a drawn line; selecting evidence always uses a published edge ID. */
export function hierarchyLines(edges: GraphEdge[]) {
  const lines: GraphEdge[] = []
  const groups = new Map<string, GraphEdge[]>()
  const byEdgeId = new Map<string, GraphEdge[]>()
  let loopCount = 0
  for (const edge of edges) {
    if (isHierarchyLoop(edge)) { loopCount++; continue }
    if (!edge.id || !edge.hierarchy_line_key) { lines.push(edge); continue }
    const key = JSON.stringify([edge.source, edge.target, edge.hierarchy_direction, edge.hierarchy_line_key])
    let group = groups.get(key)
    if (!group) {
      group = []
      groups.set(key, group)
      lines.push(edge)
    }
    group.push(edge)
    byEdgeId.set(edge.id, group)
  }
  return { lines, byEdgeId, loopCount }
}
