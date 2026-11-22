// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, reactive } from 'vue'
import type { App } from 'vue'
import DocumentLibrary from './DocumentLibrary.vue'
import type { DocumentScope, ProjectFile } from './types'

const empty = () => ({ tables: [], columns: [], source_ids: [] })
const file = (name: string, scope: DocumentScope = empty()): ProjectFile => ({
  name, file_id: name, kind: 'markdown', size: 20, relative_path: `files/${name}`, purpose: 'data_description', scope,
})
const keys = ['a.public.items', 'b.public.items']
let app: App
const root = document.createElement('div')
function mount(files: ProjectFile[], activeDocuments: string[], locked = false) {
  const state = reactive({ files, activeDocuments, tableKeys: keys, locked, busy: false })
  const save = vi.fn(), importManifest = vi.fn()
  app = createApp(defineComponent(() => () => h(DocumentLibrary, {
    ...state, 'onSave-binding': save, 'onImport-manifest': importManifest,
  })))
  document.body.append(root); app.mount(root)
  return { state, save, importManifest }
}
async function click(button: HTMLButtonElement | HTMLInputElement) { button.click(); await nextTick() }
function get<T extends Element>(selector: string, parent: ParentNode = root): T {
  const el = parent.querySelector<T>(selector)
  if (!el) throw new Error('Missing ' + selector)
  return el
}
afterEach(() => { app.unmount(); root.remove() })

describe('知识文档卡片', () => {
  it('区分待分配和全局，允许卡片内选择两个同名表；其他卡片刷新不覆盖草稿', async () => {
    const pending = { ...file('items.md'), inactive_reason: 'pending_assignment' }
    const guide = file('guide.md')
    const { state, save } = mount([pending, guide], [guide.relative_path])
    const [first, second] = root.querySelectorAll('article')
    expect(first?.textContent).toContain('待分配')
    expect(second?.textContent).toContain('全局使用')
    await click(get('.scope-toggle', first!))
    const options = first!.querySelectorAll<HTMLInputElement>('.table-option input')
    await click(options[0]!); await click(options[1]!)
    state.files = [structuredClone(pending), { ...guide, purpose: 'mixed' }]
    await nextTick()
    expect(get<HTMLSelectElement>('select', second!).value).toBe('mixed')
    expect(get('.scope-toggle', first!).textContent).toContain('已关联 2 张表')
    await click(get('.save-row .primary', first!))
    expect(save).toHaveBeenCalledWith(pending, 'data_description', { ...empty(), tables: keys })
  })
  it('单独修改用途保留原有来源和列，清除限制需显式选择', async () => {
    const scope = { source_ids: ['a'], tables: [keys[0]!], columns: ['id'] }
    const doc = file('guide.md', scope)
    const { save, state } = mount([doc], [doc.relative_path])
    const purpose = get<HTMLSelectElement>('select')
    purpose.value = 'mixed'; purpose.dispatchEvent(new Event('change')); await nextTick()
    await click(get('.save-row .primary'))
    expect(save).toHaveBeenCalledWith(doc, 'mixed', scope)
    state.locked = true; await nextTick()
    expect(purpose.disabled).toBe(true)
    expect(get<HTMLButtonElement>('.save-row .primary').disabled).toBe(true)
    await click(get('.scope-toggle'))
    expect(get<HTMLInputElement>('.table-option input').disabled).toBe(true)
    expect(root.textContent).toContain(doc.relative_path)
  })
  it('清单可以单独导入，失败可重试，成功后重置，并保留上传按钮在列表之外', async () => {
    const { importManifest } = mount([], [])
    const tools = get('.library-tools'), list = get('.document-scroll')
    expect(list.contains(tools)).toBe(false)
    await click(get('button[aria-controls="document-import"]', tools))
    const input = get<HTMLInputElement>('.manifest-field input')
    const manifest = new File(['documents: []'], 'docs.yaml')
    Object.defineProperty(input, 'files', { value: [manifest], configurable: true })
    input.dispatchEvent(new Event('change')); await nextTick()
    const form = get<HTMLFormElement>('#document-import')
    form.dispatchEvent(new Event('submit', { cancelable: true }))
    expect(importManifest).toHaveBeenCalledWith(manifest, [], expect.any(Function))
    expect(get<HTMLButtonElement>('button[type=submit]').disabled).toBe(false)
    importManifest.mock.calls[0]![2](); await nextTick()
    expect(get<HTMLButtonElement>('button[type=submit]').disabled).toBe(true)
    expect(get<HTMLButtonElement>('button[aria-controls="document-import"]').getAttribute('aria-expanded')).toBe('false')
  })
})
