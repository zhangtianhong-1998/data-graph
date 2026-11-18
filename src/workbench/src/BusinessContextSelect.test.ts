// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, ref } from 'vue'
import type { App } from 'vue'
import BusinessContextSelect from './BusinessContextSelect.vue'
import type { BusinessContext } from './types'
const api = vi.hoisted(() => ({ request: vi.fn() }))
vi.mock('./api', () => ({ projectPath: (id: string) => `/api/projects/${id}`, request: api.request }))
let app: App, root: HTMLDivElement
afterEach(() => { app?.unmount(); root?.remove(); api.request.mockReset() })
describe('business context selection', () => {
  it('pins subsequent option pages to the first publication', async () => {
    api.request.mockImplementation((url: string) => Promise.resolve(url.includes('offset=0')
      ? { version_id: 'v1', contexts: [{ project: 'M' }], support_counts: {}, next_offset: 1 }
      : { version_id: 'v1', contexts: [{ project: 'K' }], support_counts: {}, next_offset: null }))
    root = document.createElement('div'); document.body.append(root)
    app = createApp(BusinessContextSelect, { projectId: 'p', modelValue: null }); app.mount(root)
    await vi.waitFor(() => expect(root.querySelector('button')?.textContent).toContain('加载更多'))
    root.querySelector('button')!.click()
    await vi.waitFor(() => expect(root.textContent).toContain('project：K'))
    const url = new URL(api.request.mock.calls[1][0], 'http://local')
    expect(url.searchParams.get('version')).toBe('v1')
    expect(url.searchParams.get('offset')).toBe('1')
  })
  it('distinguishes all content, universal knowledge and an observed project', async () => {
    api.request.mockResolvedValue({ contexts: [{ project: 'M' }, { project: 'K' }], support_counts: { unknown: 1 }, next_offset: null })
    const selection = ref<BusinessContext | null>(null)
    root = document.createElement('div'); document.body.append(root)
    app = createApp({ setup: () => () => h(BusinessContextSelect, { projectId: 'p', version: 'old', modelValue: selection.value, 'onUpdate:modelValue': value => { selection.value = value } }) })
    app.mount(root)
    await vi.waitFor(() => expect(root.querySelectorAll('option')).toHaveLength(4))
    expect(api.request.mock.calls[0][0]).toContain('version=old')
    const select = root.querySelector('select')!
    select.value = '{"project":"M"}'; select.dispatchEvent(new Event('change')); await nextTick()
    expect(selection.value).toEqual({ project: 'M' })
    select.value = '{}'; select.dispatchEvent(new Event('change')); await nextTick()
    expect(selection.value).toEqual({})
    select.value = ''; select.dispatchEvent(new Event('change')); await nextTick()
    expect(selection.value).toBeNull()
    expect(root.textContent).toContain('部分内容尚未声明适用范围')
  })
  it('does not replace the selected history options with a late response', async () => {
    let finish!: (value: unknown) => void
    api.request.mockImplementation((url: string) => url.includes('version=old') ? new Promise(resolve => { finish = resolve }) : Promise.resolve({ contexts: [{ project: 'M' }], support_counts: {}, next_offset: null }))
    const version = ref('old')
    root = document.createElement('div'); document.body.append(root)
    app = createApp({ setup: () => () => h(BusinessContextSelect, { projectId: 'p', version: version.value, modelValue: null }) })
    app.mount(root); version.value = 'new'; await nextTick()
    await vi.waitFor(() => expect(root.textContent).toContain('project：M'))
    finish({ contexts: [{ project: 'K' }], support_counts: {}, next_offset: null }); await nextTick(); await nextTick()
    expect(root.textContent).not.toContain('project：K')
  })
})
