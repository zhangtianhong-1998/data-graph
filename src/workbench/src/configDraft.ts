import type { RunConfig } from './types'

const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value)
export function sameConfig(a: unknown, b: unknown): boolean {
  if (object(a) && object(b)) {
    const keys = Object.keys(a)
    return keys.length === Object.keys(b).length && keys.every(key => Object.hasOwn(b, key) && sameConfig(a[key], b[key]))
  }
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((item, i) => sameConfig(item, b[i]))
  return a === b
}
export function configTextChanged(text: string, baseline: string): boolean {
  try { return !sameConfig(JSON.parse(text), JSON.parse(baseline)) } catch { return true }
}

// Apply JSON edits against the snapshot the editor displayed. Unrelated form
// edits survive; arrays are one field, so source order is never guessed.
export function mergeConfigDraft(baseline: RunConfig, form: RunConfig, draft: RunConfig, resolveConflicts: 'reject' | 'draft' = 'reject'): RunConfig {
  const merge = (base: unknown, current: unknown, edited: unknown, path: string): unknown => {
    if (sameConfig(base, edited)) return current
    if (sameConfig(base, current) || sameConfig(current, edited)) return edited
    if (object(base) && object(current) && object(edited)) {
      const result: Record<string, unknown> = {}
      for (const key of new Set([...Object.keys(base), ...Object.keys(current), ...Object.keys(edited)])) {
        const value = merge(base[key], current[key], edited[key], path ? `${path}.${key}` : key)
        if (value !== undefined) Object.defineProperty(result, key, { value, enumerable: true, writable: true, configurable: true })
      }
      return result
    }
    if (resolveConflicts === 'draft') return edited
    throw new Error(`表单和完整 JSON 对同一字段的修改不同：${path}。请统一该字段，或放弃其中一处修改。`)
  }
  return merge(baseline, form, draft, '') as RunConfig
}
