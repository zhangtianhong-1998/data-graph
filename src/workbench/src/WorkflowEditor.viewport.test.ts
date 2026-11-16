// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, reactive } from 'vue'
import type { App } from 'vue'
import WorkflowEditor from './WorkflowEditor.vue'
import type { WorkflowConfig } from './types'

let root: HTMLDivElement
let app: App
let resize: () => void
let width = 420
let height = 420
let updates: WorkflowConfig[]
let state: { locked: boolean }

function button(label: string) {
  return root.querySelector<HTMLButtonElement>(`[aria-label="${label}"]`)!
}
function canvas() { return root.querySelector<HTMLElement>('.wf-canvas')! }
function scale() { return Number(root.querySelector<HTMLElement>('.wf-scene')!.style.transform.match(/scale\((.+)\)/)![1]) }

beforeEach(async () => {
  width = height = 420
  updates = []
  vi.stubGlobal('ResizeObserver', class {
    constructor(callback: () => void) { resize = callback }
    observe() {}
    disconnect() {}
  })
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(function (this: HTMLElement) {
    return this.classList.contains('wf-canvas') ? width : 0
  })
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockImplementation(function (this: HTMLElement) {
    return this.classList.contains('wf-canvas') ? height : 0
  })
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(() => ({
    x: 100, y: 50, left: 100, top: 50, right: 100 + width, bottom: 50 + height,
    width, height, toJSON: () => ({}),
  }))
  root = document.createElement('div')
  document.body.append(root)
  state = reactive({ locked: false })
  const workflow: WorkflowConfig = {
    enabled: true, max_parallel_workers: 2, enable_co_projection: false,
    units: [{ unit_id: 'extract_1', label: '抽取', source_ids: [], source_table_overrides: {}, document_ids: null, position: { x: 60, y: 40 } }],
    merge_nodes: [], edges: [], output_position: { x: 640, y: 170 },
    merge_policy: { shared_entity_types: [], authority_sources: [], conflict_mode: 'quarantine', emit_alignment_candidates: true },
  }
  app = createApp(WorkflowEditor, {
    projectId: 'test', projects: [], sources: [], tables: [], documents: [], scopes: ['public'],
    workflow, inheritedGraphs: [], get locked() { return state.locked }, dirty: false,
    'onUpdate:workflow': (value: WorkflowConfig) => updates.push(value),
  })
  app.mount(root)
  await nextTick()
})

afterEach(() => {
  app.unmount()
  root.remove()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('workflow viewport interactions', () => {
  it('fits the available canvas, refits on resize, and keeps manual zoom on resize', async () => {
    expect(scale()).toBe(0.5)
    width = 840
    resize()
    await nextTick()
    expect(scale()).toBe(1)
    button('缩小画布').click()
    await nextTick()
    expect(scale()).toBeCloseTo(1 / 1.2)
    width = 420
    resize()
    await nextTick()
    expect(scale()).toBeCloseTo(1 / 1.2)
    const fit = [...root.querySelectorAll('button')].find(b => b.textContent === '适应画布')!
    fit.click()
    await nextTick()
    expect(scale()).toBe(0.5)
    expect(updates).toEqual([]) // View changes must not dirty the workflow.
  })

  it('maps dropped nodes and moved nodes through zoom, canvas offsets and scrolling', async () => {
    canvas().scrollLeft = 50
    canvas().scrollTop = 20
    const drop = new MouseEvent('drop', { bubbles: true, clientX: 250, clientY: 150 })
    Object.defineProperty(drop, 'dataTransfer', { value: { getData: () => 'extract' } })
    canvas().dispatchEvent(drop)
    await nextTick()
    expect(updates.at(-1)!.units[1]!.position).toEqual({ x: 292, y: 191 })
    const node = root.querySelector<HTMLElement>('.wf-node-extract')!
    node.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, button: 0, clientX: 180, clientY: 100 }))
    canvas().scrollLeft = 60
    window.dispatchEvent(new MouseEvent('mousemove', { clientX: 205, clientY: 115 }))
    window.dispatchEvent(new MouseEvent('mouseup'))
    await nextTick()
    expect(updates.at(-1)!.units[0]!.position).toEqual({ x: 130, y: 70 })
  })

  it('draws wire previews in graph coordinates and connects at non-default zoom', async () => {
    canvas().scrollLeft = 50
    canvas().scrollTop = 20
    const port = root.querySelector<HTMLElement>('.wf-port-out')!
    port.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, button: 0, clientX: 200, clientY: 100 }))
    window.dispatchEvent(new MouseEvent('mousemove', { clientX: 250, clientY: 150 }))
    await nextTick()
    expect(root.querySelector('.wf-edge-preview')!.getAttribute('d')).toMatch(/^M 276 89 .* 400 240$/)
    root.querySelector('[data-wire-target="output"]')!.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }))
    await nextTick()
    expect(updates.at(-1)!.edges).toMatchObject([{ source: 'extract_1', target: 'output' }])
  })

  it('anchors wheel zoom under the pointer and pans without changing saved positions', async () => {
    canvas().scrollLeft = 50
    canvas().scrollTop = 20
    const event = new WheelEvent('wheel', { bubbles: true, cancelable: true, ctrlKey: true, deltaY: -Math.log(2) / 0.002, clientX: 250, clientY: 150 })
    // happy-dom WheelEvent derives from UIEvent and omits MouseEvent fields.
    Object.defineProperties(event, { ctrlKey: { value: true }, clientX: { value: 250 }, clientY: { value: 150 } })
    canvas().dispatchEvent(event)
    await nextTick()
    expect(event.defaultPrevented).toBe(true)
    expect(scale()).toBeCloseTo(1)
    expect(canvas().scrollLeft).toBeCloseTo(250)
    expect(canvas().scrollTop).toBeCloseTo(140)
    canvas().dispatchEvent(new MouseEvent('mousedown', { bubbles: true, button: 0, clientX: 300, clientY: 200 }))
    window.dispatchEvent(new MouseEvent('mousemove', { clientX: 270, clientY: 180 }))
    window.dispatchEvent(new MouseEvent('mouseup'))
    expect(canvas().scrollLeft).toBeCloseTo(280)
    expect(canvas().scrollTop).toBeCloseTo(160)
    expect(updates).toEqual([])
  })
})
