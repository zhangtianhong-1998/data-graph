export const modelLimitKeys = ['max_calls', 'max_prompt_tokens', 'max_output_tokens', 'timeout_seconds', 'context_window_tokens'] as const
export type ModelLimitKey = typeof modelLimitKeys[number]
export function modelLimitError(model: Record<string, unknown>): string {
  const optional = new Set<ModelLimitKey>(['max_calls', 'max_prompt_tokens', 'context_window_tokens'])
  for (const key of modelLimitKeys) {
    const value = model[key]
    if (value === undefined || value === null && optional.has(key)) continue
    if (key === 'max_calls') {
      if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) return '模型调用总额度须为非负整数，留空表示不额外限制。'
      continue
    }
    if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0 || key !== 'timeout_seconds' && !Number.isSafeInteger(value)) return '输入与输出额度须为正整数，超时时间须大于 0；可选限制可留空。'
  }
  if (typeof model.context_window_tokens === 'number' && typeof model.max_output_tokens === 'number' && model.max_output_tokens >= model.context_window_tokens) return '最大输出 tokens 必须小于模型的总上下文窗口。'
  return ''
}

export function effectiveInputTokenLimit(model: Record<string, unknown>): number | null {
  const limits: number[] = []
  if (typeof model.max_prompt_tokens === 'number') limits.push(model.max_prompt_tokens)
  if (typeof model.context_window_tokens === 'number') {
    const output = typeof model.max_output_tokens === 'number' ? model.max_output_tokens : 4096
    limits.push(Math.max(0, model.context_window_tokens - output))
  }
  return limits.length ? Math.min(...limits) : null
}
