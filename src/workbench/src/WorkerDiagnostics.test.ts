// @vitest-environment happy-dom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createApp, nextTick, reactive } from 'vue'
import type { App } from 'vue'
import WorkerDiagnostics from './WorkerDiagnostics.vue'
import type { Run } from './types'
const api = vi.hoisted(() => ({ request: vi.fn() }))
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), request: api.request }))
let app: App, root: HTMLDivElement
const run = (): Run => ({ run_id: 'r', project_id: 'p', status: 'completed', stage: 'finished', created_at: '', cleanup_status: 'completed' })
const response = () => ({ status: 'available', active: false, incomplete: false, events: [
  { code: 'worker_database_diagnostic', database: { category: 'unknown', stage: 'snapshot_start', sqlstate: 'unknown' } },
  { code: 'worker_process_exited', exit_code: 0 },
] })
async function mount(value = run()) {
  root = document.createElement('div'); document.body.append(root)
  const props = reactive({ run: value }); app = createApp(WorkerDiagnostics, props); app.mount(root); await nextTick()
  return props
}
async function open() {
  const details = root.querySelector('details')!; details.open = true; details.dispatchEvent(new Event('toggle'))
  await vi.waitFor(() => expect(root.textContent).not.toContain('正在读取'))
  return details
}
beforeEach(() => api.request.mockReset().mockResolvedValue(response()))
afterEach(() => { app?.unmount(); root?.remove(); vi.useRealTimers() })
it('loads on demand and exposes safe diagnostics and a run-specific export', async () => {
  await mount(); expect(api.request).not.toHaveBeenCalled(); await open()
  await vi.waitFor(() => expect(root.textContent).toContain('原因未确定'))
  expect(api.request.mock.calls[0][0]).toBe('/projects/p/runs/r/worker-diagnostics')
  expect(root.textContent).toContain('退出码：0')
  expect(root.querySelector('a')?.getAttribute('href')).toBe('/api/projects/p/runs/r/worker-diagnostics/export')
  expect(root.textContent).not.toContain('连接故障')
})
it('explains missing or incomplete history without inventing underlying errors', async () => {
  api.request.mockResolvedValue({ status: 'not_recorded', active: false, events: [], incomplete: false })
  await mount(); await open()
  await vi.waitFor(() => expect(root.textContent).toContain('旧运行的底层异常无法补录'))
  expect(root.querySelector('a')).toBeNull()
  api.request.mockResolvedValue({ ...response(), incomplete: true })
  root.querySelector('button')!.click()
  await vi.waitFor(() => expect(root.textContent).toContain('诊断记录不完整'))
})
it('does not poll a closed panel while a run remains active', async () => {
  await mount({ ...run(), status: 'running' }); await open()
  await vi.waitFor(() => expect(api.request).toHaveBeenCalled())
  const details = root.querySelector('details')!; details.open = false; details.dispatchEvent(new Event('toggle')); await nextTick()
  vi.useFakeTimers(); const count = api.request.mock.calls.length
  await vi.advanceTimersByTimeAsync(10000)
  expect(api.request).toHaveBeenCalledTimes(count)
})
it('shows the failed capability probe and used recovery allowance without asserting a disconnect', async () => {
  api.request.mockResolvedValue({ ...response(), events: [{ code: 'worker_database_diagnostic',
    error_code: 'database_reconnect_failed', database: { category: 'qualification', stage: 'connection_preflight',
      probe_code: 'parameter_binding_unverified', probe_failure: 'readback_mismatch',
      boundary_recovery_attempts: 3, recovery_attempt_limit: 3, terminal: true } }] })
  await mount(); await open()
  await vi.waitFor(() => expect(root.textContent).toContain('连接能力检查未通过'))
  expect(root.textContent).toContain('检查项目：参数绑定')
  expect(root.textContent).toContain('检查结果：检查结果与要求不符')
  expect(root.textContent).toContain('本次快照恢复次数：3 / 3')
  expect(root.textContent).toContain('该来源已停止事实查询')
  expect(root.textContent).toContain('检查未通过不等于网络断连')
})
it.each([
  ['query_cancel_unverified', 'cancel_not_completed', '查询取消', '取消操作未完成', 'retry_limit_exhausted', '恢复次数已耗尽'],
  ['statement_timeout_unverified', 'interrupt_not_observed', '语句超时', '未观察到预期的查询中断', 'retry_new_session', '丢弃当前会话，重新检查新会话'],
  ['runtime_control_unverified', 'malformed_row', '查询中断后的会话检查', '检查结果格式不符', 'qualification_rejected', '资格检查未通过，当前阶段不能重试'],
  ['cte_union_unverified', 'readback_mismatch', 'CTE 与 UNION 查询', '检查结果与要求不符', 'deterministic_rejection', '配置、权限或目录检查未通过'],
])('shows the original %s failure and the actual recovery decision', async (probe, failure, probeLabel, failureLabel, decision, decisionLabel) => {
  api.request.mockResolvedValue({ ...response(), events: [{ code: 'worker_database_diagnostic',
    error_code: 'database_reconnect_failed', database: { category: 'qualification', stage: 'connection_preflight',
      cause_code: probe, probe_code: probe, probe_failure: failure, recovery_decision: decision } }] })
  await mount(); await open()
  await vi.waitFor(() => expect(root.textContent).toContain(`原始错误：${probe}`))
  expect(root.textContent).toContain(`恢复判定：${decisionLabel}`)
  expect(root.textContent).toContain(`检查项目：${probeLabel}`)
  expect(root.textContent).toContain(`检查结果：${failureLabel}`)
})
