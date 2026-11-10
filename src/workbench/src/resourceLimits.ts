import type { RunConfig } from './types'

export const MiB = 1024 * 1024
export const resourceFields = [
  { key: 'max_returned_rows', id: 'max-returned-rows', label: '累计返回行数上限', unit: 1, defaultValue: 400000, unlimited: true },
  { key: 'max_returned_bytes', id: 'max-returned-mib', label: '累计返回数据量上限（MiB）', unit: MiB, defaultValue: 64 * MiB, unlimited: true },
  { key: 'max_working_bytes', id: 'max-working-mib', label: '构图工作内存上限（MiB）', unit: MiB, defaultValue: 32 * MiB, unlimited: false },
] as const
export type ResourceKey = typeof resourceFields[number]['key']
export type ResourceInputs = Record<ResourceKey, { value: number | null; unlimited: boolean }>

export function resourceInputs(config: RunConfig): ResourceInputs {
  return Object.fromEntries(resourceFields.map(field => {
    const value = config[field.key] === undefined ? field.defaultValue : config[field.key]
    return [field.key, { value: value === null ? field.defaultValue : value, unlimited: field.unlimited && value === null }]
  })) as ResourceInputs
}

export function resourceValues(inputs: ResourceInputs): Record<ResourceKey, number | null> {
  return Object.fromEntries(resourceFields.map(field => [field.key,
    field.unlimited && inputs[field.key].unlimited ? null : inputs[field.key].value])) as Record<ResourceKey, number | null>
}

export function resourceInputsError(inputs: ResourceInputs): string {
  for (const field of resourceFields) {
    const input = inputs[field.key]
    if (field.unlimited && input.unlimited) continue
    if (input.value === null || !Number.isSafeInteger(input.value) || input.value <= 0) {
      return field.unit === 1 ? '累计返回行数须为正整数，或勾选不限。' : `${field.label}须大于 0，且换算后为整数个字节。`
    }
  }
  return ''
}
