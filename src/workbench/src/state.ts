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
export const runLabels: Record<Run['status'], string> = { queued: '等待启动', running: '运行中', completed: '已完成', partial: '部分完成', failed: '失败', cancelled: '已取消', interrupted: '运行中断' }
export function graphOutcome(report: unknown): string {
  if (!report || typeof report !== 'object') return ''
  const data = report as { validation?: { business_graph?: string }; counts?: Record<string, number> }
  switch (data.validation?.business_graph) {
    case 'empty': return '当前没有已发布的业务实体或关系，请查看发现与校验问题。'
    case 'candidates_only': return '当前只有候选对象，尚未形成已确认的业务图谱。'
    case 'entities_only': return `已发布 ${data.counts?.business_nodes ?? 0} 个业务实体，尚未确认实体间关系。`
    case 'entities_and_relations': return `已发布 ${data.counts?.business_nodes ?? 0} 个业务实体、${data.counts?.business_edges ?? 0} 条业务关系。`
    case 'not_requested': return '本次仅发现规则，未执行图谱实例化。'
    case 'unavailable': return '未能读取业务图谱状态，请查看运行问题。'
    default: return ''
  }
}
export const stageLabel = (stage: string): string => ({ queued: '等待启动', starting: '初始化', metadata: '读取元数据', discovery: '发现关键列与关系', extraction: '抽取图谱', extracting: '正在执行抽取', publishing: '发布结果', completed: '处理完成', finished: '运行结束', cleanup: '清理临时数据' } as Record<string, string>)[stage] || stage || '—'
