// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import DiscoveryCandidates from './DiscoveryCandidates.vue'
const api = vi.hoisted(() => ({ request: vi.fn() }))
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), request: api.request }))
let app: ReturnType<typeof createApp>, root: HTMLDivElement
afterEach(() => { app?.unmount(); root?.remove(); api.request.mockReset() })
it('请求重试使用配置和候选修订，不提供直接发布操作', async () => {
  const candidate = { candidate_id: 'candidate_a', source: { source_id: 's', schema: 'public', table: 'items' }, failure_code: 'semantic_claim_unresolved', column_refs: ['c1'], document_refs: ['doc@1:s'], blocking_evidence_refs: [], required_evidence_refs: [], state: 'pending', revision: 3 }
  api.request.mockResolvedValue({ candidates: [candidate], total_candidates: 1, next_offset: null })
  root = document.createElement('div'); document.body.append(root)
  app = createApp(DiscoveryCandidates, { projectId: 'p', configRevision: '"4"' }); app.mount(root)
  await vi.waitFor(() => expect(root.textContent).toContain('业务含义尚不能确认'))
  expect(root.textContent).not.toContain('接受并发布')
  const button = [...root.querySelectorAll('button')].find(b => b.textContent === '请求重新探索')!
  button.click(); await nextTick()
  expect(api.request).toHaveBeenCalledWith('/projects/p/discovery/decisions', {
    method: 'POST', headers: { 'If-Match': '"4"' }, body: JSON.stringify({ candidate_id: 'candidate_a', revision: 3, action: 'retry' }),
  })
})

it('已拒绝提案不显示待办或恢复入口，只允许用户明确重新调查', async () => {
  const candidate = { candidate_id: 'candidate_rejected', source: { source_id: 's', schema: 'public', table: '产品定义' }, proposal_kind: 'reference', failure_code: 'reference_contract_unresolved', failure_details: ['relation_endpoint_kind_mismatch'], column_refs: ['c1'], document_refs: [], blocking_evidence_refs: [], required_evidence_refs: [], state: 'rejected', revision: 2 }
  api.request.mockResolvedValue({ candidates: [candidate], total_candidates: 1, next_offset: null })
  root = document.createElement('div'); document.body.append(root)
  app = createApp(DiscoveryCandidates, { projectId: 'p', configRevision: '"4"' }); app.mount(root)
  await vi.waitFor(() => expect(root.textContent).toContain('已拒绝，本次提案已结束'))
  expect(root.querySelector('h2')?.textContent).toContain('发现提案与处理记录')
  expect(root.textContent).toContain('所选关系不适用于这两类对象')
  expect(root.textContent).toContain('普通续跑不会重新打开这份提案')
  const buttons = [...root.querySelectorAll<HTMLButtonElement>('.discovery-item button')]
  expect(buttons.map(button => button.textContent)).toEqual(['重新调查'])
  expect(api.request).toHaveBeenCalledTimes(1)
  buttons[0]!.click(); await nextTick()
  expect(api.request).toHaveBeenCalledWith('/projects/p/discovery/decisions', {
    method: 'POST', headers: { 'If-Match': '"4"' }, body: JSON.stringify({ candidate_id: 'candidate_rejected', revision: 2, action: 'retry' }),
  })
})
