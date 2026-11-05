import type { Run } from './types'

export const isActive = (run?: Run | null): boolean => !!run && ['queued', 'running'].includes(run.status)
export const splitLines = (value: string): string[] => value.split('\n').map(line => line.trim()).filter(Boolean)
export function assertNoCredentials(value: unknown): void {
  if (!value || typeof value !== 'object') return
  for (const [key, item] of Object.entries(value)) {
    if (['api_key', 'password', 'access_token', 'secret', 'dsn'].includes(key.toLowerCase()) && item) {
      throw new Error('配置不能包含明文密钥或连接串，请使用 api_key_env / dsn_env 引用服务器环境变量。')
    }
    assertNoCredentials(item)
  }
}
export const runLabels: Record<Run['status'], string> = { queued: '等待启动', running: '运行中', completed: '已完成', completed_with_gaps: '已完成，有未决项', partial: '执行未完成', failed: '失败', cancelled: '已取消', interrupted: '运行中断' }
export function graphOutcome(report: unknown): string {
  if (!report || typeof report !== 'object') return ''
  const data = report as { validation?: { business_graph?: string; construction_target?: string }; counts?: Record<string, number> }
  if (data.validation?.construction_target === 'structure') {
    const count = data.counts?.structures_published ?? 0
    return count > 0 ? `已发布 ${count} 份结构与映射方案；本次未生成或验证具体实例。` : '本次目标是结构与映射，尚无已发布方案；请查看结构检查结果。'
  }
  switch (data.validation?.business_graph) {
    case 'empty': return '当前没有已发布的业务实体或关系，请查看发现与校验问题。'
    case 'candidates_only': return '当前只有候选对象，尚未形成已确认的业务图谱。'
    case 'entities_only': return `已发布 ${data.counts?.business_nodes ?? 0} 个业务实体，尚未确认实体间关系。`
    case 'entities_and_relations': return `已发布 ${data.counts?.business_nodes ?? 0} 个业务实体、${data.counts?.business_edges ?? 0} 条业务关系。`
    case 'not_requested': return '本次仅发现规则，未执行图谱实例化。'
    case 'unavailable': return '未能读取业务图谱状态，请查看运行问题。'
    default: {
      // Earlier construction reports have business counts but no legacy graph
      // classification. Counts describe published output, not task acceptance.
      const nodes = data.counts?.business_nodes, edges = data.counts?.business_edges
      if (Number.isSafeInteger(nodes) && nodes! > 0 && Number.isSafeInteger(edges) && edges! >= 0) {
        return edges! > 0 ? `已发布 ${nodes} 个业务实体、${edges} 条业务关系。` : `已发布 ${nodes} 个业务实体，尚未确认实体间关系。`
      }
      return ''
    }
  }
}
export const stageLabel = (stage: string): string => ({ queued: '等待启动', starting: '初始化', initializing: '初始化', metadata: '读取元数据', discovery: '发现对象与属性', validation: '验证规则', relations: '发现补充关系', extraction: '抽取图谱', extracting: '正在执行抽取', publishing: '发布结果', completed: '处理完成', finished: '运行结束', cleanup: '释放运行资源' } as Record<string, string>)[stage] || stage || '—'
