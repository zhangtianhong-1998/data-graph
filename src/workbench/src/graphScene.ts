import type { Graph, GraphNode } from './types'
import { mergeDisplaySchema } from './graphLabels'

export type Point = { x: number; y: number }
export const edgeKey = (edge: Graph['edges'][number]) => String(edge.id || JSON.stringify([edge.source, edge.target, edge.relation, edge.qualifiers, edge.status]))

/** Pages are transport batches. Existing nodes/edges stay on the same scene. */
export function mergeScene(previous: Graph, incoming: Graph): Graph {
  if (previous.generation && incoming.generation && previous.generation !== incoming.generation) previous = { nodes: [], edges: [] }
  const nodes = new Map(previous.nodes.map(n => [n.id, n]))
  for (const node of incoming.nodes) nodes.set(node.id, node)
  const edges = new Map(previous.edges.filter(e => nodes.has(e.source) && nodes.has(e.target)).map(e => [edgeKey(e), e]))
  for (const edge of incoming.edges) if (nodes.has(edge.source) && nodes.has(edge.target)) edges.set(edgeKey(edge), edge)
  return { ...previous, ...incoming, display_schema: mergeDisplaySchema(previous.display_schema, incoming.display_schema), nodes: [...nodes.values()], edges: [...edges.values()] }
}

/** For a complete tree, allocate one angular sector per branch and use rings
 * for distance from its root. Mixed networks use the directed bands below. */
function radialTree(graph: Graph, ids: Set<string>): Map<string, Point> | undefined {
  if (ids.size < 8 || graph.edges.length !== ids.size - 1 || graph.truncated || graph.next_offset != null
      || Number(graph.total_nodes ?? ids.size) > ids.size) return
  const neighbors = new Map<string, string[]>(Array.from(ids, id => [id, []]))
  const sources = new Set<string>(), targets = new Set<string>()
  for (const edge of graph.edges) {
    if (!neighbors.has(edge.source) || !neighbors.has(edge.target) || edge.source === edge.target) return
    neighbors.get(edge.source)!.push(edge.target)
    neighbors.get(edge.target)!.push(edge.source)
    sources.add(edge.source); targets.add(edge.target)
  }
  const roots = [...ids].filter(id => !targets.has(id))
  const sinks = [...ids].filter(id => !sources.has(id))
  const root = String(graph.focus_node_id ?? (roots.length === 1 ? roots[0] : sinks.length === 1 ? sinks[0] : ''))
  if (!ids.has(root)) return
  const queue = [root], depth = new Map([[root, 0]])
  const children = new Map<string, string[]>()
  for (let i = 0; i < queue.length; i++) {
    const id = queue[i]!, next = neighbors.get(id)!.filter(child => !depth.has(child)).sort()
    for (const child of next) {
      depth.set(child, depth.get(id)! + 1)
      queue.push(child)
    }
    children.set(id, next)
  }
  if (depth.size !== ids.size || queue.length !== ids.size) return
  const maxDepth = depth.get(queue[queue.length - 1]!)!
  // A chain reads more naturally along the horizontal direction.
  if (maxDepth > 2 * Math.sqrt(ids.size)) return
  const leaves = new Map<string, number>()
  for (let i = queue.length - 1; i >= 0; i--) {
    const id = queue[i]!
    leaves.set(id, children.get(id)!.reduce((sum, child) => sum + leaves.get(child)!, 0) || 1)
  }
  const sectors = new Map([[root, 0]])
  const angles = new Map<string, number>(), rings: number[][] = []
  for (const id of queue) {
    let start = sectors.get(id)!
    const angle = 2 * Math.PI * (start + leaves.get(id)! / 2) / leaves.get(root)!
    angles.set(id, angle)
    const level = depth.get(id)!
    if (!rings[level]) rings[level] = []
    rings[level]!.push(angle)
    for (const child of children.get(id)!) {
      sectors.set(child, start)
      start += leaves.get(child)!
    }
  }
  const radii = [0]
  for (let level = 1; level <= maxDepth; level++) {
    const ring = rings[level]!.sort((a, b) => a - b)
    let minGap = 2 * Math.PI
    for (let i = 0; i < ring.length; i++) {
      const next = ring[i + 1] ?? ring[0]! + 2 * Math.PI
      minGap = Math.min(minGap, next - ring[i]!)
    }
    radii[level] = Math.max(radii[level - 1]! + 145, 155 / minGap)
  }
  const center = radii[maxDepth]! + 100, result = new Map<string, Point>()
  for (const id of queue) {
    const radius = radii[depth.get(id)!]!, angle = angles.get(id)! - Math.PI / 2
    result.set(id, { x: center + radius * Math.cos(angle), y: center + radius * Math.sin(angle) })
  }
  return result
}

/** Directed breadth-first bands; existing positions survive every expansion.
 * Wide levels wrap within their band so a large star is not a 4000-row strip.
 */
export function placeNodes(graph: Graph, previous: Map<string, Point> = new Map()): Map<string, Point> {
  const ids = new Set(graph.nodes.map(n => n.id))
  const points = new Map([...previous].filter(([id]) => ids.has(id)))
  if (points.size === ids.size) return points
  if (!points.size) {
    const tree = radialTree(graph, ids)
    if (tree) return tree
  }
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
      let y = 70 + (slot % rows) * 140
      // New bands may grow, but never move an already visible object.
      while (occupied.has(`${x}:${y}`)) y += 140
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
