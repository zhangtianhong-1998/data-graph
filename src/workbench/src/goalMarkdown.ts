/** Compatibility rendering only; Markdown never grants table or column access. */
import type { RunConfig } from './types'
export function goalMarkdown(config: RunConfig): string {
  if (typeof config.goal_markdown === 'string') return config.goal_markdown
  const goal = config.goal || {}, parts: string[] = []
  if (goal.purpose?.trim()) parts.push(goal.purpose.trim())
  const sections: Array<[string, string[] | undefined]> = [
    ['重点关注', goal.include], ['排除内容', goal.exclude], ['补充说明', goal.discovery_guidance],
    ['运行指导', goal.runtime_guidance?.map(item => item.text)],
    ['指定对象类型', goal.entity_kinds], ['指定关系类型', goal.relation_kinds],
  ]
  for (const [heading, values] of sections) if (values?.length) parts.push(`## ${heading}\n\n${values.join('\n\n')}`)
  return parts.join('\n\n')
}
