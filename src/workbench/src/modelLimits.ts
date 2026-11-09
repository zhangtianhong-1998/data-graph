export const modelLimitKeys = ['max_calls', 'max_prompt_tokens', 'max_output_tokens', 'timeout_seconds', 'context_window_tokens'] as const
// Match ModelConfig and RunConfig defaults. Explicit null remains an opt-out.
export const modelLimitDefaults = { max_calls: 2000, max_prompt_tokens: 1048576, max_output_tokens: 65536, timeout_seconds: 800 } as const
export const runSecondsDefault = 60000
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
  const input = model.max_prompt_tokens === undefined ? modelLimitDefaults.max_prompt_tokens : model.max_prompt_tokens
  if (typeof input === 'number') limits.push(input)
  if (typeof model.context_window_tokens === 'number') {
    const output = typeof model.max_output_tokens === 'number' ? model.max_output_tokens : modelLimitDefaults.max_output_tokens
    limits.push(Math.max(0, model.context_window_tokens - output))
  }
  return limits.length ? Math.min(...limits) : null
}
