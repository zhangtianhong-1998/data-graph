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
