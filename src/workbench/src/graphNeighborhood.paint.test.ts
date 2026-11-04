// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import type { App } from 'vue'
import GraphCanvas from './GraphCanvas.vue'
import type { Graph } from './types'
import { placeNodes, sceneBounds } from './graphScene'
import { fitCamera } from './graphViewport'

type PaintedNode = { x: number; y: number; color: string }
let app: App | undefined, root: HTMLDivElement
let frames: PaintedNode[][]
let context: CanvasRenderingContext2D

beforeEach(() => {
  frames = []
  const drawing = {
    fillStyle: '', strokeStyle: '', font: '', lineWidth: 1, globalAlpha: 1, textAlign: 'center',
    setTransform: vi.fn(), translate: vi.fn(), scale: vi.fn(),
    clearRect: vi.fn(() => { frames.push([]) }),
    setLineDash: vi.fn(), beginPath: vi.fn(), moveTo: vi.fn(), lineTo: vi.fn(),
    stroke: vi.fn(), fill: vi.fn(), fillText: vi.fn(),
    measureText: (text: string) => ({ width: text.length * 13 }),
    arc: vi.fn((x: number, y: number) => { frames.at(-1)!.push({ x, y, color: drawing.fillStyle }) }),
  }
  context = drawing as unknown as CanvasRenderingContext2D
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(context)
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
})
afterEach(() => { app?.unmount(); root?.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('two-hop Canvas painting', () => {
  it('clears the old bitmap when 401 nodes become 301, then restores the full scene', async () => {
    const related = Array.from({ length: 301 }, (_, i) => ({ id: `n${String(i).padStart(3, '0')}`, label: `关联节点${i}`, kind: '关联对象' }))
    const unrelated = Array.from({ length: 100 }, (_, i) => ({ id: `z${i}`, label: `无关节点${i}`, kind: '无关对象' }))
    const graph: Graph = { nodes: [...related, ...unrelated], edges: related.slice(1).map(node => ({ source: related[0]!.id, target: node.id })) }
    root = document.createElement('div'); document.body.append(root)
    app = createApp(GraphCanvas, { graph, layer: 'business' }); app.mount(root)
    await nextTick(); await nextTick()
    const bitmap = root.querySelector('canvas')!
    expect(bitmap).not.toBeNull()
    await vi.waitFor(() => expect(frames.at(-1)).toHaveLength(401))
    const before = frames.at(-1)!
    expect(before).toHaveLength(401)
    expect(before.filter(node => node.color === '#20a27e')).toHaveLength(100)

    vi.spyOn(bitmap, 'getBoundingClientRect').mockReturnValue({ x: 0, y: 0, left: 0, top: 0, right: 820, bottom: 480, width: 820, height: 480, toJSON() {} })
    const positions = placeNodes(graph), center = positions.get(related[0]!.id)!
    const camera = fitCamera(sceneBounds(positions), { width: 820, height: 480 })
    const clearCount = vi.mocked(context.clearRect).mock.calls.length
    bitmap.dispatchEvent(new MouseEvent('dblclick', { bubbles: true, detail: 2,
      clientX: center.x * camera.zoom + camera.offset.x,
      clientY: center.y * camera.zoom + camera.offset.y }))
    await vi.waitFor(() => expect(root.querySelector('[aria-label="两跳关联范围"]')).not.toBeNull())
    await nextTick(); await nextTick()

    // Both scenes remain above the Canvas threshold: this exercises a repaint,
    // not replacing the old bitmap with a newly mounted SVG.
    expect(root.querySelector('canvas')).toBe(bitmap)
    expect(vi.mocked(context.clearRect).mock.calls.length).toBeGreaterThan(clearCount)
    const focused = frames.at(-1)!
    expect(focused).toHaveLength(301)
    expect(focused.every(node => node.color === '#407ff2')).toBe(true)
    expect(root.textContent).toContain('两跳范围已加载 301 / 301 个节点')

    const back = [...root.querySelectorAll('button')].find(button => button.textContent?.trim() === '返回全图')!
    back.click(); await nextTick(); await nextTick()
    expect(root.querySelector('canvas')).toBe(bitmap)
    expect(root.querySelector('[aria-label="两跳关联范围"]')).toBeNull()
    expect(frames.at(-1)).toEqual(before)
    expect(root.textContent).toContain('画布已加载 401 / 全图 401 个节点')
  })
})
