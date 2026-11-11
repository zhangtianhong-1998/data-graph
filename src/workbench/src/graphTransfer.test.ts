import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './api'
import { newGraphTransfer, transferGraph } from './graphTransfer'
import type { Graph } from './types'
const api = vi.hoisted(() => ({ request: vi.fn() }))
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), request: api.request }))
const nodes = Array.from({ length: 5001 }, (_, i) => ({ id: `n${i}`, label: `节点${i}` }))
const edges = Array.from({ length: 17003 }, (_, i) => ({ id: `e${i}`, source: `n${i % 5000}`, target: `n${i % 5000 + 1}` }))
function page(path: string) {
  const url = new URL(path, 'http://local'), part = url.searchParams.get('part')!, offset = Number(url.searchParams.get('offset'))
  const data = part === 'nodes' ? nodes : edges, values = data.slice(offset, offset + 1000)
  return { generation: 'same', part, offset, next_offset: offset + values.length < data.length ? offset + values.length : null, nodes: part === 'nodes' ? values : [], edges: part === 'edges' ? values : [], total_nodes: nodes.length, total_edges: edges.length, matched_nodes: nodes.length, matched_edges: edges.length }
}
beforeEach(() => { api.request.mockReset() })
describe('full graph transport', () => {
  it('loads every node and every edge separately, including connections across node batches', async () => {
    api.request.mockImplementation(async path => page(path))
    const state = newGraphTransfer(), progress: Graph[] = []
    const result = await transferGraph('p', { layer: 'business' }, state, new AbortController().signal, value => progress.push(value))
    expect(result.nodes).toHaveLength(5001); expect(result.edges).toHaveLength(17003)
    expect(result.edges.some(e => e.source === 'n999' && e.target === 'n1000')).toBe(true)
    expect(result.fully_loaded).toBe(true); expect(result.truncated).toBe(false)
    expect(progress.slice(0, -1).every(g => g.fully_loaded === false)).toBe(true)
    expect(api.request).toHaveBeenCalledTimes(24)
    expect(api.request.mock.calls.slice(1).every(call => call[0].includes('generation=same'))).toBe(true)
  })
  it('pause aborts outstanding reads and resumes at the first unaccepted batch', async () => {
    const controller = new AbortController(), state = newGraphTransfer()
    api.request.mockImplementation(async path => page(path))
    await expect(transferGraph('p', { layer: 'business' }, state, controller.signal, () => controller.abort())).rejects.toMatchObject({ code: 'request_cancelled' })
    expect(state.nodes.size).toBe(1000); expect(state.complete).toBe(false)
    const result = await transferGraph('p', { layer: 'business' }, state, new AbortController().signal, () => {})
    expect(api.request.mock.calls[1][0]).toContain('offset=1000')
    expect(result.nodes).toHaveLength(5001); expect(result.edges).toHaveLength(17003)
  })
  it('rejects publication changes without mixing new edges into old nodes', async () => {
    api.request.mockImplementation(async path => ({ ...page(path), generation: path.includes('offset=1000') ? 'new' : 'same' }))
    const state = newGraphTransfer()
    await expect(transferGraph('p', { layer: 'business' }, state, new AbortController().signal, () => {})).rejects.toMatchObject({ code: 'graph_generation_changed' })
    expect(state.nodes.size).toBe(1000); expect(state.complete).toBe(false)
  })
  it('reports a count mismatch instead of declaring an incomplete response complete', async () => {
    api.request.mockResolvedValue({ nodes: [{ id: 'a' }], edges: [], generation: 'g', part: 'nodes', offset: 0, next_offset: null, matched_nodes: 2, matched_edges: 0 })
    await expect(transferGraph('p', { layer: 'business' }, newGraphTransfer(), new AbortController().signal, () => {})).rejects.toMatchObject({ code: 'graph_batch_incomplete' })
  })
  it('retains completed batches after network failure so retry does not lose progress', async () => {
    let fail = true
    api.request.mockImplementation(async path => { if (fail && path.includes('offset=1000')) throw new ApiError('network_unavailable'); return page(path) })
    const state = newGraphTransfer()
    await expect(transferGraph('p', { layer: 'business' }, state, new AbortController().signal, () => {})).rejects.toMatchObject({ code: 'network_unavailable' })
    expect(state.nodes.size).toBe(1000); fail = false
    const result = await transferGraph('p', { layer: 'business' }, state, new AbortController().signal, () => {})
    expect(result.nodes).toHaveLength(5001)
  })
  it('loads a large two-hop view with its own selection-bound cursor', async () => {
    api.request.mockImplementation(async path => page(path))
    const result = await transferGraph('p', { layer: 'business', focus: 'n0', traversal: 'all', hops: 2 }, newGraphTransfer(), new AbortController().signal, () => {})
    expect(api.request.mock.calls.every(call => call[0].includes('focus=n0') && call[0].includes('hops=2') && call[0].includes('traversal=all'))).toBe(true)
    expect(result.nodes).toHaveLength(5001); expect(result.neighborhood_truncated).toBe(false)
  })
  it('accepts byte-limited pages smaller than the requested count without dropping any endpoint', async () => {
    api.request.mockImplementation(async path => {
      const response = page(path), items = response.part === 'nodes' ? nodes : edges
      const values = items.slice(response.offset, response.offset + 100)
      return { ...response, nodes: response.part === 'nodes' ? values : [], edges: response.part === 'edges' ? values : [],
        next_offset: response.offset + values.length < items.length ? response.offset + values.length : null }
    })
    const result = await transferGraph('p', { layer: 'business' }, newGraphTransfer(), new AbortController().signal, () => {})
    expect(result.nodes).toHaveLength(5001); expect(result.edges).toHaveLength(17003)
    expect(api.request.mock.calls[1][0]).toContain('offset=100')
  })
  it('discards a bad edge page atomically then resumes at its unchanged cursor', async () => {
    let corrupt = true
    api.request.mockImplementation(async path => {
      const response = page(path)
      return corrupt && response.part === 'edges' ? { ...response, edges: [...response.edges.slice(0, -1), { id: 'bad', source: 'n0', target: 'missing' }] } : response
    })
    const state = newGraphTransfer()
    await expect(transferGraph('p', { layer: 'business' }, state, new AbortController().signal, () => {})).rejects.toMatchObject({ code: 'graph_batch_invalid' })
    expect(state.part).toBe('edges'); expect(state.offset).toBe(0); expect(state.edges.size).toBe(0)
    expect(state.graph.nodes).toHaveLength(5001)
    corrupt = false
    const result = await transferGraph('p', { layer: 'business' }, state, new AbortController().signal, () => {})
    expect(result.edges).toHaveLength(17003); expect(result.fully_loaded).toBe(true)
  })
  it('retries a transient read at the same cursor and still limits repeated failures', async () => {
    let failures = 2
    api.request.mockImplementation(async path => {
      if (path.includes('offset=1000') && failures-- > 0) throw new ApiError('http_503', 503)
      return page(path)
    })
    const state = newGraphTransfer()
    const result = await transferGraph('p', { layer: 'business' }, state, new AbortController().signal, () => {})
    expect(result.fully_loaded).toBe(true)
    expect(api.request.mock.calls.slice(1, 4).map(call => call[0])).toEqual(Array(3).fill(api.request.mock.calls[1][0]))
    api.request.mockReset().mockRejectedValue(new ApiError('request_timeout'))
    await expect(transferGraph('p', { layer: 'business' }, newGraphTransfer(), new AbortController().signal, () => {})).rejects.toMatchObject({ code: 'request_timeout' })
    expect(api.request).toHaveBeenCalledTimes(3)
  })
})
