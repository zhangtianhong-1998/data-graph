// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive } from 'vue'
import type { App as VueApp } from 'vue'
import StructurePreview from './StructurePreview.vue'
import App from './App.vue'
import { ApiError } from './api'

const mocks = vi.hoisted(() => ({ request: vi.fn() }))
vi.mock('./api', async () => ({ ...await vi.importActual('./api'), request: mocks.request, ensureSession: vi.fn().mockResolvedValue(undefined) }))
vi.mock('./GraphCanvas.vue', () => ({ default: { template: '<div data-testid="instance-canvas">实例画布</div>' } }))
vi.mock('./IdentityCandidates.vue', () => ({ default: { template: '<div />' } }))
vi.mock('./DiscoveryCandidates.vue', () => ({ default: { template: '<div />' } }))

let app: VueApp, root: HTMLDivElement
const props = reactive({ projectId: 'p', configRevision: '1' })
function plan(revision: number) {
  return { draft_id: 'full', revision, validation_id: `check-${revision}`, checked_at: `2026-11-05T10:00:0${revision}+00:00`, passed: true, historical: revision === 1, is_current_revision: revision === 2, entity_type_count: revision === 1 ? 9 : 1, relation_type_count: revision === 1 ? 5 : 0, mapping_count: revision === 1 ? 14 : 1 }
}
function result(revision = 1) {
  const item = plan(revision)
  const entities = Array.from({ length: item.entity_type_count }, (_, index) => ({ type: `类型${index}`, description: `类型说明${index}`, key_names: ['编号'], mappings: [{ source_id: 's', table: 'items', key_columns: ['code'] }] }))
  const relations = Array.from({ length: item.relation_type_count }, (_, index) => ({ type: `关系${index}`, source_type: `类型${index}`, target_type: `类型${index + 1}`, description: `关系说明${index}`, mappings: [{ source_id: 's', table: 'items' }] }))
  return { target: 'graph_structure', plans: [plan(2), plan(1)], data_validation: 'not_run', instance_enumeration: 'not_run', selected: { ...item, draft: { entities, relations, purpose: '保留完整结构方案' }, bindings: [{ source_id: 's', schema: 'public', table: 'items', columns: ['code'] }], diagnostics: [] as Array<{ code: string }> } }
}
const empty = { target: 'graph_structure', plans: [], selected: null, data_validation: 'not_run', instance_enumeration: 'not_run' }
function button(label: string) { return [...root.querySelectorAll('button')].find(item => item.textContent?.trim() === label)! }
function expectSeparatedLabels() {
  const readBox = (element: Element) => Object.fromEntries(['x', 'y', 'width', 'height'].map(key => [key, Number(element.getAttribute(key))])) as { x: number; y: number; width: number; height: number }
  const labels = [...root.querySelectorAll('.relation-label-box')].map(readBox)
  const nodes = [...root.querySelectorAll('[data-entity-type] > rect')].map(readBox)
  const overlap = (a: typeof labels[number], b: typeof labels[number]) => a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y
  labels.forEach((label, index) => {
    expect(labels.slice(index + 1).some(other => overlap(label, other))).toBe(false)
    expect(nodes.some(node => overlap(label, node))).toBe(false)
  })
}
function mount(fullApp = false) {
  root = document.createElement('div'); document.body.append(root)
  app = fullApp ? createApp(App) : createApp({ render: () => h(StructurePreview, props) })
  app.mount(root)
}
beforeEach(() => { props.projectId = 'p'; props.configRevision = '1'; mocks.request.mockReset(); sessionStorage.clear() })
afterEach(() => { app?.unmount(); root?.remove(); vi.restoreAllMocks(); sessionStorage.clear() })

describe('结构方案预览', () => {
  it.each(['crossing', 'shared-endpoints'])('交叉关系名称在曲线上错开，不覆盖其他文字或节点：%s', async pattern => {
    const response = result()
    const pairs = pattern === 'crossing' ? [[0, 4], [0, 5], [1, 5], [1, 6], [2, 7]] : Array.from({ length: 5 }, () => [0, 4])
    response.selected.draft.relations.forEach((relation, index) => {
      relation.type = `relation_variant_${index}`
      relation.source_type = `类型${pairs[index]![0]}`; relation.target_type = `类型${pairs[index]![1]}`
    })
    mocks.request.mockResolvedValue(response); mount()
    await vi.waitFor(() => expect(root.querySelectorAll('.relation-label-box')).toHaveLength(5))
    expectSeparatedLabels()
    expect(root.querySelectorAll('[data-numbered="true"]')).toHaveLength(0)
  })

  it('超长名称用短编号关联完整关系表，自关联标签仍避开节点', async () => {
    const response = result()
    const relation = response.selected.draft.relations[0]!
    relation.type = '关联类别'.repeat(25); relation.target_type = relation.source_type
    response.selected.draft.relations = [relation]
    mocks.request.mockResolvedValue(response); mount()
    await vi.waitFor(() => expect(root.querySelectorAll('.relation-label-box')).toHaveLength(1))
    expectSeparatedLabels()
    expect(root.querySelector('.relation-label')?.textContent).toBe('#1')
    const table = root.querySelector('[aria-label="关系定义"]')!
    expect(table.textContent).toContain(relation.type)
    expect(table.textContent).toContain('#1')
  })

  it('顶部和两侧类型的自关系曲线与标签均留在画布范围内', async () => {
    const response = result()
    response.selected.draft.relations.forEach((relation, index) => {
      relation.type = `self_relation_${index}`
      relation.source_type = `类型${[0, 2, 7, 3, 6][index]}`; relation.target_type = relation.source_type
    })
    mocks.request.mockResolvedValue(response); mount()
    await vi.waitFor(() => expect(root.querySelectorAll('.relation-label-box')).toHaveLength(5))
    expectSeparatedLabels()
    const [, , width, height] = root.querySelector('svg')!.getAttribute('viewBox')!.split(' ').map(Number)
    for (const path of root.querySelectorAll('[data-relation-type] > path')) {
      const coordinates = path.getAttribute('d')!.match(/-?\d+(?:\.\d+)?/g)!.map(Number)
      coordinates.forEach((value, index) => { expect(value).toBeGreaterThanOrEqual(0); expect(value).toBeLessThanOrEqual(index % 2 ? height! : width!) })
    }
    for (const rect of root.querySelectorAll('.relation-label-box')) {
      const x = Number(rect.getAttribute('x')), y = Number(rect.getAttribute('y'))
      expect(x).toBeGreaterThanOrEqual(0); expect(y).toBeGreaterThanOrEqual(0)
      expect(x + Number(rect.getAttribute('width'))).toBeLessThanOrEqual(width!)
      expect(y + Number(rect.getAttribute('height'))).toBeLessThanOrEqual(height!)
    }
  })

  it('展示历史的9类实体和5类关系，不把结构方案当作零实例失败', async () => {
    mocks.request.mockResolvedValue(result())
    mount()
    await vi.waitFor(() => expect(root.textContent).toContain('9 类实体 · 5 类关系'))
    expect(root.querySelectorAll('[data-entity-type]')).toHaveLength(9)
    expect(root.querySelectorAll('[data-relation-type]')).toHaveLength(5)
    expect(root.textContent).toContain('结构检查已通过')
    expect(root.textContent).toContain('历史修订，仅供查看，不会替换当前方案')
    expect(root.textContent).toContain('不是具体实例节点')
    expect(root.textContent).not.toContain('0 个节点')
    expect(root.querySelector('table[aria-label="实体类型定义"] tbody')?.children).toHaveLength(9)
    expect(root.querySelector('table[aria-label="关系定义"] tbody')?.children).toHaveLength(5)
    const svg = root.querySelector('svg')!, width = Number(svg.getAttribute('width'))
    root.querySelector<HTMLButtonElement>('[aria-label="放大结构图"]')!.click(); await nextTick()
    expect(Number(svg.getAttribute('width'))).toBeGreaterThan(width)
  })

  it('只读选择历史修订，不提交草稿或重新查询实例', async () => {
    mocks.request.mockResolvedValueOnce(result(2)).mockResolvedValueOnce(result(1))
    mount()
    await vi.waitFor(() => expect(root.textContent).toContain('1 类实体 · 0 类关系'))
    const select = root.querySelector<HTMLSelectElement>('select')!
    select.value = 'check-1'; select.dispatchEvent(new Event('change', { bubbles: true }))
    await vi.waitFor(() => expect(root.textContent).toContain('9 类实体 · 5 类关系'))
    expect(mocks.request.mock.calls[1]?.[0]).toBe('/projects/p/graph/structure?draft_id=full&revision=1')
    expect(mocks.request.mock.calls.every(call => !call[1]?.method || call[1].method === 'GET')).toBe(true)
  })

  it('无结构检查时说明缺少记录，不报抽取失败', async () => {
    mocks.request.mockResolvedValue(empty); mount()
    await vi.waitFor(() => expect(root.textContent).toContain('尚无结构检查记录'))
    expect(root.querySelector('svg')).toBeNull()
    expect(root.textContent).not.toContain('抽取失败')
  })

  it('未通过的结构检查仍可查看，但保留诊断和未通过状态', async () => {
    const response = result(); response.selected.passed = false; response.selected.diagnostics = [{ code: 'unauthorized_column' }]
    mocks.request.mockResolvedValue(response); mount()
    await vi.waitFor(() => expect(root.textContent).toContain('结构检查未通过'))
    expect(root.querySelector('[aria-label="结构检查诊断"]')?.textContent).toContain('unauthorized_column')
    expect(root.querySelectorAll('[data-entity-type]')).toHaveLength(9)
  })

  it('配置权限变更立即清除旧方案，错误响应不会保留旧内容', async () => {
    mocks.request.mockResolvedValueOnce(result()).mockRejectedValueOnce(new ApiError('graph_access_context_changed', 400))
    mount(); await vi.waitFor(() => expect(root.textContent).toContain('保留完整结构方案'))
    props.configRevision = '2'; await nextTick()
    await vi.waitFor(() => expect(root.querySelector('[role="alert"]')?.textContent).toContain('访问范围已变化'))
    expect(root.textContent).not.toContain('保留完整结构方案')
    expect(root.querySelector('svg')).toBeNull()
  })

  it('切换项目后忽略旧项目迟到的响应', async () => {
    let finish!: (value: unknown) => void
    mocks.request.mockReturnValueOnce(new Promise(resolve => { finish = resolve })).mockResolvedValueOnce(empty)
    mount(); props.projectId = 'other'; await nextTick()
    await vi.waitFor(() => expect(root.textContent).toContain('尚无结构检查记录'))
    finish(result()); await nextTick(); await nextTick()
    expect(root.textContent).not.toContain('保留完整结构方案')
    expect(root.querySelector('svg')).toBeNull()
  })

  it('业务图无实例时也能独立打开结构，不改变原实例查询', async () => {
    const project = { project_id: 'p', name: '结构预览', files: [], created_at: '', updated_at: '' }
    mocks.request.mockImplementation(async (path: string) => {
      if (path === '/projects') return { projects: [project] }
      if (path === '/projects/p') return project
      if (path.endsWith('/config')) return { model: { mode: 'disabled' }, goal_markdown: '发现结构', sources: [] }
      if (path.endsWith('/credentials')) return { database_sources: [], model: false }
      if (path.endsWith('/metadata')) return { tables: [], nodes: [], edges: [] }
      if (path.endsWith('/runs')) return { runs: [] }
      if (path.includes('/graph?')) return { nodes: [], edges: [] }
      if (path.endsWith('/graph/structure')) return result()
      throw new Error(`Unexpected ${path}`)
    })
    mount(true)
    await vi.waitFor(() => expect(button('运行与结果')).toBeDefined())
    button('运行与结果').click(); await nextTick(); button('业务图谱').click(); await nextTick()
    const graphCalls = mocks.request.mock.calls.filter(call => call[0].includes('/graph?')).length
    expect(root.querySelector('[aria-label="结构方案预览"]')).toBeNull()
    button('查看结构方案').click()
    await vi.waitFor(() => expect(root.textContent).toContain('9 类实体 · 5 类关系'))
    expect(root.querySelector('[data-testid="instance-canvas"]')).not.toBeNull()
    button('收起结构方案').click(); await nextTick()
    expect(root.querySelector('[aria-label="结构方案预览"]')).toBeNull()
    expect(mocks.request.mock.calls.filter(call => call[0].includes('/graph?'))).toHaveLength(graphCalls)
  })
})
