import { describe, expect, it } from 'vitest'
import { mergeScene, placeNodes } from './graphScene'

describe('continuous scene', () => {
  it('places siblings in separate sectors for both directions of a rooted tree', () => {
    const nodes = Array.from({length: 13}, (_, i) => ({ id: String(i) }))
    const edges = nodes.slice(1).map((node, i) => ({source: String(Math.floor(i / 3)), target: node.id}))
    const positions = placeNodes({nodes, edges})
    const reversed = placeNodes({nodes, edges: edges.map(edge => ({source: edge.target, target: edge.source}))})
    expect(reversed).toEqual(positions)
    const root = positions.get('0')!
    const distance = (id: string) => Math.hypot(positions.get(id)!.x - root.x, positions.get(id)!.y - root.y)
    expect(distance('1')).toBeCloseTo(distance('2'))
    expect(distance('12')).toBeGreaterThan(distance('1'))
    expect(new Set([...positions.values()].map(p => `${p.x},${p.y}`)).size).toBe(nodes.length)
  })
  it('retains nodes, boundary edges and positions across neighbour batches', () => {
    const initial = { generation: 'a', nodes: [{ id: 'root' }, { id: 'one' }], edges: [{ source: 'root', target: 'one' }] }
    const points = placeNodes(initial)
    const next = mergeScene(initial, { generation: 'a', nodes: [{ id: 'one' }, { id: 'two' }], edges: [{ source: 'one', target: 'two' }] })
    expect(next.nodes.map(n => n.id)).toEqual(['root', 'one', 'two'])
    expect(next.edges).toHaveLength(2)
    const expanded = placeNodes(next, points)
    expect(expanded.get('root')).toEqual(points.get('root'))
    expect(expanded.get('one')).toEqual(points.get('one'))
    expect(expanded.get('two')!.x).toBeGreaterThan(expanded.get('one')!.x)
    expect(mergeScene(next, { ...initial, generation: 'b' }).nodes).toHaveLength(2)
  })
  it('retains all nodes and edges beyond the old limits, including cycles and disconnected nodes', () => {
    const nodes = Array.from({ length: 5005 }, (_, i) => ({ id: String(i) }))
    expect(mergeScene({ nodes: [], edges: [] }, { nodes, edges: [] }).nodes).toHaveLength(5005)
    const graph = { nodes: nodes.slice(0, 3), edges: [{ source: '0', target: '1' }, { source: '1', target: '0' }] }
    expect(placeNodes(graph).size).toBe(3)
    const dense = { ...graph, edges: Array.from({ length: 17003 }, (_, i) => ({ id: String(i), source: '0', target: '1' })) }
    expect(mergeScene({ nodes: [], edges: [] }, dense).edges).toHaveLength(17003)
  })
})
