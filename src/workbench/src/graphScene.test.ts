import { describe, expect, it } from 'vitest'
import { mergeScene, placeNodes, SCENE_LIMIT, SCENE_EDGE_LIMIT } from './graphScene'

describe('continuous scene', () => {
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
  it('bounds loaded nodes and keeps cycles/disconnected nodes visible', () => {
    const nodes = Array.from({ length: SCENE_LIMIT + 5 }, (_, i) => ({ id: String(i) }))
    expect(mergeScene({ nodes: [], edges: [] }, { nodes, edges: [] }).nodes).toHaveLength(SCENE_LIMIT)
    const graph = { nodes: nodes.slice(0, 3), edges: [{ source: '0', target: '1' }, { source: '1', target: '0' }] }
    expect(placeNodes(graph).size).toBe(3)
    const dense = { ...graph, edges: Array.from({ length: SCENE_EDGE_LIMIT + 3 }, (_, i) => ({ id: String(i), source: '0', target: '1' })) }
    expect(mergeScene({ nodes: [], edges: [] }, dense).edges).toHaveLength(SCENE_EDGE_LIMIT)
  })
})
