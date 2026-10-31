import type { Graph, GraphNode } from './types'

export const SCENE_LIMIT = 4000
export const SCENE_EDGE_LIMIT = 16000
export type Point = { x: number; y: number }
export const edgeKey = (edge: Graph['edges'][number]) => String(edge.id || JSON.stringify([edge.source, edge.target, edge.relation, edge.qualifiers, edge.status]))

/** Pages are transport batches. Existing nodes/edges stay on the same scene. */
export function mergeScene(previous: Graph, incoming: Graph): Graph {
  if (previous.generation && incoming.generation && previous.generation !== incoming.generation) previous = { nodes: [], edges: [] }
  const nodes = new Map(previous.nodes.slice(0, SCENE_LIMIT).map(n => [n.id, n]))
  for (const node of incoming.nodes) if (nodes.has(node.id) || nodes.size < SCENE_LIMIT) nodes.set(node.id, node)
  const edges = new Map(previous.edges.filter(e => nodes.has(e.source) && nodes.has(e.target)).slice(0, SCENE_EDGE_LIMIT).map(e => [edgeKey(e), e]))
  for (const edge of incoming.edges) if (nodes.has(edge.source) && nodes.has(edge.target) && (edges.has(edgeKey(edge)) || edges.size < SCENE_EDGE_LIMIT)) edges.set(edgeKey(edge), edge)
  return { ...previous, ...incoming, nodes: [...nodes.values()], edges: [...edges.values()] }
}

/** Directed breadth-first bands; existing positions survive every expansion.
 * Wide levels wrap within their band so a large star is not a 4000-row strip.
 */
export function placeNodes(graph: Graph, previous: Map<string, Point> = new Map()): Map<string, Point> {
  const ids = new Set(graph.nodes.map(n => n.id))
  const points = new Map([...previous].filter(([id]) => ids.has(id)))
  const outgoing = new Map<string, string[]>(), indegree = new Set<string>()
  for (const edge of graph.edges) if (ids.has(edge.source) && ids.has(edge.target) && edge.source !== edge.target) {
    const children = outgoing.get(edge.source) || []
    children.push(edge.target); outgoing.set(edge.source, children); indegree.add(edge.target)
  }
  const sorted = [...graph.nodes].sort((a, b) => `${a.kind}:${a.id}`.localeCompare(`${b.kind}:${b.id}`))
  const queue: Array<[string, number]> = sorted.filter(n => !indegree.has(n.id)).map(n => [n.id, 0])
  const depths = new Map<string, number>()
  let index = 0
  const traverse = () => {
    while (index < queue.length) {
      const [id, depth] = queue[index++]!
      if (depths.has(id)) continue
      depths.set(id, depth)
      for (const target of outgoing.get(id) || []) queue.push([target, depth + 1])
    }
  }
  traverse()
  // Cycles still appear; the drawing does not assert an inferred hierarchy.
  for (const node of sorted) if (!depths.has(node.id)) { queue.push([node.id, 0]); traverse() }
  const layers = new Map<number, string[]>()
  for (const node of sorted) { const depth = depths.get(node.id)!; const layer = layers.get(depth) || []; layer.push(node.id); layers.set(depth, layer) }
  const rows = Math.max(4, Math.ceil(Math.sqrt(graph.nodes.length)))
  const occupied = new Set([...points.values()].map(p => `${p.x}:${p.y}`))
  let left = 90
  for (const depth of [...layers.keys()].sort((a,b) => a-b)) {
    const layer = layers.get(depth)!
    layer.forEach((id, slot) => {
      if (points.has(id)) return
      const x = left + Math.floor(slot / rows) * 180
      let y = 70 + (slot % rows) * 110
      // New bands may grow, but never move an already visible object.
      while (occupied.has(`${x}:${y}`)) y += 110
      points.set(id, { x, y }); occupied.add(`${x}:${y}`)
    })
    left += Math.ceil(layer.length / rows) * 180 + 50
  }
  return points
}

export function sceneBounds(points: Map<string, Point>) {
  let width = 180, height = 140
  for (const p of points.values()) { width = Math.max(width, p.x + 100); height = Math.max(height, p.y + 100) }
  return { width, height }
}
export function hitNode(nodes: GraphNode[], points: Map<string, Point>, x: number, y: number, radius: number) {
  return nodes.find(node => { const p = points.get(node.id); return p && Math.hypot(p.x - x, p.y - y) <= radius })
}
