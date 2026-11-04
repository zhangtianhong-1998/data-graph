import type { Graph } from './types'

export type NeighborhoodTraversal = 'chains' | 'all'

/** No-backend fallback; retain any incompleteness warning from the input graph. */
export function twoHopNeighborhood(graph: Graph, center: string, traversal: NeighborhoodTraversal = 'all'): Graph {
  const available = new Set(graph.nodes.map(node => node.id))
  const incoming = new Map<string, Set<string>>(), outgoing = new Map<string, Set<string>>()
  for (const edge of graph.edges) {
    if (!available.has(edge.source) || !available.has(edge.target)) continue
    if (!outgoing.has(edge.source)) outgoing.set(edge.source, new Set())
    if (!incoming.has(edge.target)) incoming.set(edge.target, new Set())
    outgoing.get(edge.source)!.add(edge.target)
    incoming.get(edge.target)!.add(edge.source)
  }
  const ids = new Set(available.has(center) ? [center] : [])
  // Keep independent visited sets: a node reachable in both directions must
  // continue each chain. Never turn through a shared parent/owner in chains mode.
  for (const directions of traversal === 'chains' ? [[incoming], [outgoing]] : [[incoming, outgoing]]) {
    const visited = new Set(available.has(center) ? [center] : [])
    let frontier = [...visited]
    for (let hop = 0; hop < 2; hop++) {
      const next: string[] = []
      for (const id of frontier) for (const neighbors of directions) for (const neighbor of neighbors.get(id) || []) {
        if (!visited.has(neighbor)) { visited.add(neighbor); ids.add(neighbor); next.push(neighbor) }
      }
      frontier = next
    }
  }
  return { ...graph, nodes: graph.nodes.filter(node => ids.has(node.id)), edges: graph.edges.filter(edge => ids.has(edge.source) && ids.has(edge.target)), focus_node_id: center, focus_hops: 2, focus_traversal: traversal, focus_neighbor_count: Math.max(0, ids.size - 1), next_offset: null }
}
