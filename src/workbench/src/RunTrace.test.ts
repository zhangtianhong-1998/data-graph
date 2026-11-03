// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, reactive } from 'vue'
import type { App } from 'vue'
import RunTrace from './RunTrace.vue'
import type { Run } from './types'
const api = vi.hoisted(() => ({ request: vi.fn() }))
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), request: api.request }))
let app: App, root: HTMLDivElement
const run = (): Run => ({ run_id: 'run_a', project_id: 'p', status: 'completed', stage: 'finished', created_at: '2026-11-02T01:00:00Z', cleanup_status: 'completed' })
const event = (seq: number) => ({ seq, kind: 'model_request', time: '2026-11-02T01:00:00Z', data: { messages: [{ role: 'user', content: `input_${seq}` }], tools: [{ name: 'query' }] } })
const page = (seq = 1) => ({ events: [event(seq)], total_events: 2, next_offset: seq === 1 ? 1 : null, status: 'available', active: false })
const button = (name: string) => [...root.querySelectorAll('button')].find(b => b.textContent?.trim() === name)!
async function mount(value = run()) {
  const props = reactive({ run: value, configRevision: '"7"' })
  root = document.createElement('div'); document.body.append(root); app = createApp(RunTrace, props); app.mount(root)
  await vi.waitFor(() => expect(button('刷新轨迹').disabled).toBe(false))
  return props
}
beforeEach(() => { api.request.mockReset().mockResolvedValue(page()) })
afterEach(() => { app?.unmount(); root?.remove(); vi.restoreAllMocks(); vi.useRealTimers() })
describe('persistent run trace viewer', () => {
  it('shows exact recorded inputs on demand and exposes unpaged native export', async () => {
    await mount()
    expect(api.request.mock.calls[0][0]).toBe('/projects/p/runs/run_a/trace?offset=0&limit=10')
    expect(root.querySelector('pre')).toBeNull()
    const details = root.querySelector<HTMLDetailsElement>('.trace-step')!
    details.open = true; details.dispatchEvent(new Event('toggle')); await nextTick()
    expect(root.querySelector('pre')!.textContent).toContain('input_1')
    expect(root.querySelector('pre')!.textContent).toContain('tools')
    expect(root.querySelector('a')!.getAttribute('href')).toBe('/api/projects/p/runs/run_a/trace/export')
    expect(root.textContent).toContain('运行结束后保留')
    expect(api.request.mock.calls.every(([, options]) => !options?.method || options.method === 'GET')).toBe(true)
  })
  it('uses cursors for next and previous pages without truncating the export', async () => {
    api.request.mockImplementation(async path => page(path.includes('offset=1') ? 2 : 1))
    await mount(); button('下一页轨迹').click()
    await vi.waitFor(() => expect(root.textContent).toContain('第 2 步'))
    expect(button('下一页轨迹').disabled).toBe(true)
    button('上一页轨迹').click()
    await vi.waitFor(() => expect(root.textContent).toContain('第 1 步'))
  })
  it('deletes only after explicit confirmation and preserves the report contract', async () => {
    api.request.mockImplementation(async (_path, options) => options?.method === 'DELETE' ? {} : page())
    await mount(); button('清理轨迹').click(); await nextTick()
    expect(root.querySelector('[role="alertdialog"]')).not.toBeNull()
    expect(api.request.mock.calls.some(([, options]) => options?.method === 'DELETE')).toBe(false)
    api.request.mockImplementation(async (_path, options) => options?.method === 'DELETE' ? {} : { ...page(), events: [], total_events: 0, next_offset: null, status: 'cleared' })
    button('确认删除').click()
    await vi.waitFor(() => expect(root.textContent).toContain('轨迹已手动清理'))
    const removal = api.request.mock.calls.find(([, options]) => options?.method === 'DELETE')!
    expect(removal[1].headers).toEqual({ 'If-Match': '"7"' })
    expect(root.textContent).toContain('运行报告和图谱结果仍保留')
    expect(root.querySelector('a')).toBeNull()
  })
  it('keeps active traces, polls current inputs, and never clears on completion', async () => {
    const props = await mount({ ...run(), status: 'running', stage: 'discovery' })
    expect(button('清理轨迹').disabled).toBe(true)
    expect(root.textContent).toContain('截至下载时已写入')
    vi.useFakeTimers(); button('刷新轨迹').click(); await nextTick(); await Promise.resolve(); await nextTick()
    const calls = api.request.mock.calls.length
    await vi.advanceTimersByTimeAsync(3001)
    expect(api.request.mock.calls.length).toBeGreaterThan(calls)
    props.run.status = 'completed'; await nextTick(); await Promise.resolve(); await nextTick()
    expect(api.request.mock.calls.every(([, options]) => options?.method !== 'DELETE')).toBe(true)
  })
  it('distinguishes legacy missing records from manually cleared records', async () => {
    api.request.mockResolvedValue({ events: [], total_events: 0, next_offset: null, status: 'not_recorded', active: false })
    await mount()
    expect(root.textContent).toContain('历史输入无法从汇总报告中恢复')
    expect(button('清理轨迹').disabled).toBe(true)
    expect(root.querySelector('a')).toBeNull()
  })
  it('marks trace write failures and exports only what was recorded', async () => {
    api.request.mockResolvedValue({ ...page(), incomplete: true, error_code: 'trace_write_failed' })
    await mount()
    expect(root.querySelector('[role="alert"]')!.textContent).toContain('部分事件写入失败')
    expect(root.querySelector('a')!.textContent).toBe('导出已记录轨迹')
    expect(root.textContent).toContain('不能据此还原全部执行过程')
  })
  it('does not replace the next run with a late response from the previous run', async () => {
    let finish!: (value: unknown) => void
    const props = await mount()
    api.request.mockImplementation(path => path.includes('run_a') ? new Promise(resolve => { finish = resolve }) : Promise.resolve(page(2)))
    button('刷新轨迹').click(); await nextTick()
    props.run.run_id = 'run_b'
    await vi.waitFor(() => expect(root.textContent).toContain('第 2 步'))
    finish(page(1)); await nextTick(); await nextTick()
    expect(root.textContent).not.toContain('第 1 步')
    expect(root.querySelector('a')!.getAttribute('href')).toContain('run_b')
  })
})
