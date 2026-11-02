// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive } from 'vue'
import type { App } from 'vue'
import RunProgress from './RunProgress.vue'
import type { Run, RunEvent } from './types'

const event = (seq: number, code = 'table_completed'): RunEvent => ({ seq, time: '2026-11-02T06:01:00Z', stage: 'discovery', code, level: 'info', table: `fruit.public.table_${seq}` })
function run(): Run {
  return {
    run_id: 'run_one', project_id: 'first', status: 'running', stage: 'extracting', created_at: '2026-11-02T06:00:00Z', cleanup_status: 'pending',
    progress: { stage: 'discovery', completed: 1, total: 5, unit: 'tables', updated_at: '2026-11-02T06:01:00Z', elapsed_seconds: 60, counters: { model_calls: 3, input_tokens: 1200, output_tokens: 300 }, events: [event(1)], dropped_events: 0 },
  }
}
let app: App | undefined
let root: HTMLDivElement
async function render(initial = run()) {
  const state = reactive({ run: initial, refreshError: false })
  root = document.createElement('div'); document.body.append(root)
  app = createApp({ setup: () => () => h(RunProgress, state) }); app.mount(root)
  await flush()
  return state
}
async function flush() { await nextTick(); await nextTick() }
function button(): HTMLButtonElement { return root.querySelector('.runtime-log-head button')! }
function scrollArea() {
  const log = root.querySelector<HTMLElement>('[role="log"]')!
  Object.defineProperties(log, { scrollHeight: { value: 900, configurable: true }, clientHeight: { value: 300, configurable: true } })
  return log
}
afterEach(() => { app?.unmount(); app = undefined; root?.remove(); vi.useRealTimers() })

describe('运行进度与过程日志', () => {
  it('只用后端明确的任务总量计算阶段进度，明确区别全流程覆盖率', async () => {
    const state = await render()
    const progress = root.querySelector('[role="progressbar"]')!
    expect(progress.getAttribute('aria-valuenow')).toBe('20')
    expect(root.textContent).toContain('1 / 5 张表 · 20%')
    expect(root.textContent).toContain('不代表全流程或图谱覆盖率')
    state.run.progress!.completed = 3
    await flush()
    expect(progress.getAttribute('aria-valuenow')).toBe('60')
    expect(root.textContent).toContain('1,200')
  })

  it('阶段没有总量时不编造百分比，等待模型返回也保留当前阶段', async () => {
    const initial = run()
    initial.progress!.completed = null; initial.progress!.total = null
    initial.progress!.events = [event(1, 'model_call_started')]
    await render(initial)
    const progress = root.querySelector('[role="progressbar"]')!
    expect(progress.hasAttribute('aria-valuenow')).toBe(false)
    expect(progress.classList.contains('indeterminate')).toBe(true)
    expect(root.textContent).toContain('发现对象与属性')
    expect(root.textContent).toContain('等待返回')
  })

  it('不把过期或非法计数显示成超过100%的进度', async () => {
    const initial = run(); initial.progress!.completed = 7
    await render(initial)
    expect(root.querySelector('[role="progressbar"]')?.hasAttribute('aria-valuenow')).toBe(false)
    expect(root.textContent).not.toContain('140%')
  })

  it.each(['partial', 'failed', 'cancelled', 'interrupted'] as const)('%s 终态不显示成功的100%进度', async status => {
    const initial = run(); initial.status = status; initial.stage = 'finished'; initial.progress!.completed = 5
    await render(initial)
    expect(root.querySelector('[role="progressbar"]')).toBeNull()
    expect(root.textContent).not.toContain('100%')
  })

  it('完成但有未决项与成功完成使用不同状态文案', async () => {
    const initial = run(); initial.status = 'completed_with_gaps'; initial.progress!.events.push(event(2, 'run_completed_with_gaps'))
    await render(initial)
    expect(root.textContent).toContain('已完成，有未决项')
    expect(root.textContent).toContain('未决项与已发布结果请查看下方报告')
    expect(root.textContent).toContain('抽取已结束，仍有未决项')
  })

  it('旧运行没有进度快照时保留状态且说明无法补回历史日志', async () => {
    const initial = run(); delete initial.progress
    await render(initial)
    expect(root.textContent).toContain('旧版本启动的运行无法补回实时日志')
    expect(root.textContent).toContain('等待后端上报进度')
    expect(root.querySelector('[role="log"]')).toBeNull()
  })

  it('默认跟随新日志，用户向上滚动后保持阅读位置，继续跟随回到底部', async () => {
    const state = await render()
    const log = scrollArea()
    state.run.progress!.events.push(event(2))
    await flush()
    expect(log.scrollTop).toBe(900)
    log.scrollTop = 100; log.dispatchEvent(new Event('scroll')); await flush()
    expect(button().textContent).toBe('继续跟随')
    state.run.progress!.events.push(event(3)); await flush()
    expect(log.scrollTop).toBe(100)
    expect(root.textContent).toContain('table_3')
    button().click(); await flush()
    expect(log.scrollTop).toBe(900)
    expect(button().textContent).toBe('暂停跟随')
  })

  it('手动暂停只停止滚动跟随，日志仍然更新，终态事件可见', async () => {
    const state = await render()
    const log = scrollArea(); log.scrollTop = 50
    button().click(); await flush()
    state.run.progress!.events.push(event(2, 'run_failed')); state.run.status = 'failed'
    await flush()
    expect(log.scrollTop).toBe(50)
    expect(root.textContent).toContain('抽取失败')
    expect(log.getAttribute('aria-live')).toBe('off')
  })

  it('切换运行不混入旧日志，并恢复新运行的默认跟随', async () => {
    const state = await render()
    button().click(); await flush()
    const second = run(); second.run_id = 'run_two'; second.project_id = 'second'; second.progress!.events = [event(50)]
    state.run = second; await flush()
    expect(root.textContent).not.toContain('table_1')
    expect(root.textContent).toContain('table_50')
    expect(button().textContent).toBe('暂停跟随')
  })

  it('只展示最近200条并说明省略数量，表名中的HTML按纯文本展示', async () => {
    const initial = run(); initial.progress!.events = Array.from({ length: 210 }, (_, i) => event(i + 1)); initial.progress!.dropped_events = 40
    initial.progress!.events[209]!.table = '<img src=x onerror="window.stolen=true">'
    await render(initial)
    expect(root.querySelectorAll('.runtime-log-row')).toHaveLength(200)
    expect(root.textContent).toContain('较早的 50 条已省略')
    expect(root.querySelector('img')).toBeNull()
    expect(root.textContent).toContain('<img src=x')
  })

  it('刷新失败时保留日志并标明记录已过期，恢复后清除提示', async () => {
    const state = await render()
    state.refreshError = true; await flush()
    expect(root.textContent).toContain('当前显示上次收到的记录')
    expect(root.textContent).toContain('table_1')
    expect(root.querySelector('[role="progressbar"]')?.getAttribute('aria-valuetext')).toContain('刷新失败')
    state.refreshError = false; state.run.progress!.events.push(event(2)); await flush()
    expect(root.textContent).not.toContain('当前显示上次收到的记录')
    expect(root.textContent).toContain('table_2')
  })

  it('运行用时随时钟更新，时钟不会推进阶段百分比，卸载清除计时器', async () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date('2026-11-02T06:01:00Z'))
    await render()
    expect(root.textContent).toContain('1 分 0 秒')
    await vi.advanceTimersByTimeAsync(15000)
    expect(root.textContent).toContain('1 分 15 秒')
    expect(root.querySelector('[role="progressbar"]')?.getAttribute('aria-valuenow')).toBe('20')
    app?.unmount(); app = undefined
    expect(vi.getTimerCount()).toBe(0)
  })
})
