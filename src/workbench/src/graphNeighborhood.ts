import type { Graph } from './types'

/** No-backend fallback; retain any incompleteness warning from the input graph. */
export function twoHopNeighborhood(graph: Graph, center: string): Graph {
  const available = new Set(graph.nodes.map(node => node.id))
  const neighbors = new Map<string, Set<string>>()
  for (const edge of graph.edges) {
    if (!available.has(edge.source) || !available.has(edge.target)) continue
    if (!neighbors.has(edge.source)) neighbors.set(edge.source, new Set())
    if (!neighbors.has(edge.target)) neighbors.set(edge.target, new Set())
    neighbors.get(edge.source)!.add(edge.target)
    neighbors.get(edge.target)!.add(edge.source)
  }
  const ids = new Set(available.has(center) ? [center] : [])
  let frontier = [...ids]
  for (let hop = 0; hop < 2; hop++) {
    const next: string[] = []
    for (const id of frontier) for (const neighbor of neighbors.get(id) || []) {
      if (!ids.has(neighbor)) { ids.add(neighbor); next.push(neighbor) }
    }
    frontier = next
  }
  return { ...graph, nodes: graph.nodes.filter(node => ids.has(node.id)), edges: graph.edges.filter(edge => ids.has(edge.source) && ids.has(edge.target)), focus_node_id: center, focus_hops: 2, focus_neighbor_count: Math.max(0, ids.size - 1), next_offset: null }
}
