// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp } from 'vue'
import type { App } from 'vue'
import GraphCanvas from './GraphCanvas.vue'
import type { Graph, StandardNode } from './types'

const api = vi.hoisted(() => ({ request: vi.fn() }))
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), request: api.request }))

let app: App
let root: HTMLDivElement

beforeEach(() => {
  api.request.mockReset()
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
})

afterEach(() => {
  app?.unmount()
  root?.remove()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('标准化节点详情面板 (NodeInspector)', () => {
  it('按标准化名片、业务描述/物理目录注释分离、动态元属性与多物理表来源卡展示节点', async () => {
    const standardNode: StandardNode = {
      id: 'n_prod_1',
      kind: 'Product',
      status: 'accepted',
      chinese_name: '企业级云专线',
      english_name: 'Enterprise Cloud Line',
      english_name_status: 'mapped',
      display_name: '企业级云专线',
      aliases: ['云专线'],
      identity: { product_code: 'P001' },
      identity_types: { product_code: 'text' },
      business_description: '面向政企客户的云网融合产品',
      business_description_origin: 'source_column',
      catalog_metadata_notes: [
        {
          source_id: 'gauss_main',
          qualified_table: 'dm.dma_fin_income_f',
          table_comment: '收入事实宽表',
          column_comments: [{ column: 'unit_name', data_type: 'character varying', comment: '计量单位编码名称' }],
          note_kind: 'physical_catalog_declaration',
        },
      ],
      attributes: [
        {
          key: 'unit',
          label: '计量单位',
          values: ['万元', { '范围': '国内' }],
          data_type: 'character varying',
          origin_tables: [{ source_id: 'gauss_main', qualified_table: 'dm.dma_fin_income_f', column: 'unit_name', data_type: 'character varying' }],
        },
      ],
      source_evidences: [
        {
          source_id: 'gauss_main',
          source_kind: 'gauss_database',
          source_kind_label: 'GaussDB',
          schema: 'dm',
          table: 'dma_fin_income_f',
          qualified_table: 'dm.dma_fin_income_f',
          key_columns: ['prod_code'],
          contributed_fields: [
            { role: 'chinese_name', field: '主名称', column: 'prod_cn', data_type: 'varchar' },
            { role: 'english_name', field: '英文名', column: 'prod_en', data_type: 'varchar' },
          ],
          scopes: ['public'],
          evidence_refs: ['unit_income:e1'],
          inherited_from: null,
        },
        {
          source_id: 'gauss_main',
          source_kind: 'gauss_database',
          source_kind_label: 'GaussDB',
          schema: 'dm',
          table: 'dma_fin_ebg_order_f',
          qualified_table: 'dm.dma_fin_ebg_order_f',
          key_columns: ['product_cd'],
          contributed_fields: [
            { role: 'description', field: '节点描述', column: 'prod_desc', data_type: 'text' },
          ],
          scopes: ['public'],
          evidence_refs: ['unit_ebg:e4'],
          inherited_from: { inherit_id: 'base_v1', source_project_id: 'p_old_income' },
        },
      ],
      scopes: ['public'],
    }

    api.request.mockResolvedValue({
      facts: [{ id: 'n_prod_1', fact_kind: 'NodeExistence', status: 'accepted', payload: { entity_id: 'n_prod_1', kind: 'Product' } }],
      supports: [],
      rules: [],
      nodes: [{ id: 'n_prod_1', label: '企业级云专线', kind: 'Product' }],
      standard_nodes: [standardNode],
      next_offset: null,
      supports_truncated: false,
    })

    const graph: Graph = {
      nodes: [{ id: 'n_prod_1', label: '企业级云专线', kind: 'Product', layer: 'business', status: 'accepted', identity: { product_code: 'P001' } }],
      edges: [],
    }
    root = document.createElement('div')
    document.body.append(root)
    app = createApp(GraphCanvas, { graph, layer: 'business', projectId: 'p' })
    app.mount(root)

    root.querySelector('.graph-node')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await vi.waitFor(() => expect(root.querySelectorAll('.source-evidence-card')).toHaveLength(2))

    const text = root.querySelector('.graph-detail')!.textContent || ''
    expect(text).toContain('企业级云专线')
    expect(text).toContain('Enterprise Cloud Line')
    expect(text).toContain('面向政企客户的云网融合产品')
    expect(text).toContain('物理目录注释')
    expect(text).toContain('收入事实宽表')
    expect(text).toContain('character varying')
    expect(root.querySelector('.dynamic-attr-item')?.textContent).toContain('国内')
    expect(root.querySelector('.dynamic-attr-item')?.textContent).not.toContain('[object Object]')
    expect(text).toContain('dm.dma_fin_income_f')
    expect(text).toContain('dm.dma_fin_ebg_order_f')
    expect(text).toContain('unit_income:e1')
    expect(text).toContain('unit_ebg:e4')
    expect(text).toContain('p_old_income')
    expect([...root.querySelectorAll('.graph-detail pre')].every(node => !!node.closest('details:not([open])'))).toBe(true)
  })

  it('旧图缺少独立英文名列时明确显示未知，不强猜别名', async () => {
    const legacyStd: StandardNode = {
      id: 'n_old',
      kind: 'Metric',
      status: 'accepted',
      chinese_name: '营业收入',
      english_name: null,
      english_name_status: 'unknown',
      display_name: '营业收入',
      aliases: ['RevenueAlias'],
      identity: { code: 'M01' },
      identity_types: { code: 'text' },
      business_description: null,
      business_description_origin: 'none',
      catalog_metadata_notes: [],
      attributes: [],
      source_evidences: [],
      scopes: ['public'],
    }
    api.request.mockResolvedValue({
      facts: [{ id: 'n_old', fact_kind: 'NodeExistence', status: 'accepted', payload: { entity_id: 'n_old', kind: 'Metric' } }],
      supports: [],
      rules: [],
      standard_nodes: [legacyStd],
      next_offset: 50,
      supports_truncated: false,
    })

    root = document.createElement('div')
    document.body.append(root)
    app = createApp(GraphCanvas, {
      graph: { nodes: [{ id: 'n_old', label: '营业收入', kind: 'Metric', status: 'accepted', aliases: ['RevenueAlias'] }], edges: [] },
      layer: 'business',
      projectId: 'p',
    })
    app.mount(root)

    root.querySelector('.graph-node')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await vi.waitFor(() => expect(root.querySelector('.standard-node-summary')).not.toBeNull())
    const card = root.querySelector('.standard-node-summary')!.textContent || ''
    expect(card).toContain('未知（未单独映射）')
    expect(card).toContain('RevenueAlias')
    expect([...root.querySelectorAll('.node-inspector > .property-table th')].map(el => el.textContent)).not.toContain('aliases')

    // Later pages can contain only edge facts and other endpoints.
    api.request.mockResolvedValueOnce({
      facts: [], supports: [], rules: [], standard_nodes: [{ ...legacyStd, id: 'neighbor' }],
      next_offset: null, supports_truncated: false,
    })
    const more = [...root.querySelectorAll('button')].find(button => button.textContent === '读取更多事实支持')!
    more.click()
    await vi.waitFor(() => expect(api.request).toHaveBeenCalledTimes(2))
    await vi.waitFor(() => expect(root.querySelector('.standard-node-summary')?.textContent).toContain('RevenueAlias'))
  })
})
