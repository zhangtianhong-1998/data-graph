<script setup lang="ts">
import type { GuidanceStage, RuntimeGuidance } from './types'
const props = defineProps<{ modelValue: RuntimeGuidance[]; disabled?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: RuntimeGuidance[]] }>()
const stages: { value: GuidanceStage; label: string }[] = [
  { value: 'T0_discovery', label: '初步发现' }, { value: 'T1_table_discovery', label: '表内补查' },
  { value: 'T2_cross_table', label: '关系发现' }, { value: 'T3_hierarchy', label: '层级分析' },
  { value: 'T4_document', label: '文档分析' }, { value: 'T5_revision', label: '反例修订' },
  { value: 'semantic_review', label: '语义复核' },
]
function allowed(stage: GuidanceStage, injection: RuntimeGuidance['injection']) {
  return injection === 'global' || injection === 'semantic_review' && stage === 'semantic_review' || injection === 'task' && stage !== 'semantic_review' || injection === 'iteration' && !['semantic_review', 'T4_document'].includes(stage)
}
function change(index: number, patch: Partial<RuntimeGuidance>) {
  if (patch.injection) patch.stages = props.modelValue[index]!.stages.filter(stage => allowed(stage, patch.injection!))
  emit('update:modelValue', props.modelValue.map((entry, i) => i === index ? { ...entry, ...patch } : entry))
}
function toggle(index: number, stage: GuidanceStage, checked: boolean) {
  const current = props.modelValue[index]!
  change(index, { stages: checked ? [...current.stages, stage] : current.stages.filter(item => item !== stage) })
}
</script>

<template>
  <section class="field runtime-guidance">
    <div class="panel-head"><h3>运行时指导</h3><button class="btn small" :disabled="disabled || modelValue.length >= 16" @click="emit('update:modelValue', [...modelValue, { text: '', injection: 'global', stages: [] }])">添加指导</button></div>
    <p class="field-hint">全局指导会传给每个新 Agent；每轮指导会在发现和修订时重新传入。可用 Markdown 编写，上传的知识文档仍作为证据使用。</p>
    <fieldset v-for="(entry, index) in modelValue" :key="index" :disabled="disabled" class="guidance-entry">
      <legend>指导 {{ index + 1 }}</legend>
      <label :for="`guidance-injection-${index}`">生效位置</label>
      <select :id="`guidance-injection-${index}`" :value="entry.injection" @change="change(index, { injection: ($event.target as HTMLSelectElement).value as RuntimeGuidance['injection'] })">
        <option value="global">全局</option><option value="task">指定任务</option><option value="iteration">每轮发现与修订</option><option value="semantic_review">独立复核</option>
      </select>
      <label :for="`guidance-text-${index}`">指导内容</label>
      <textarea :id="`guidance-text-${index}`" :value="entry.text" maxlength="4000" @input="change(index, { text: ($event.target as HTMLTextAreaElement).value })"/>
      <details><summary>限定阶段（不选则使用上述位置的全部阶段）</summary><label v-for="stage in stages.filter(item => allowed(item.value, entry.injection))" :key="stage.value" class="checkbox-line"><input type="checkbox" :checked="entry.stages.includes(stage.value)" @change="toggle(index, stage.value, ($event.target as HTMLInputElement).checked)"/>{{ stage.label }}</label></details>
      <button class="btn small danger-text" @click="emit('update:modelValue', modelValue.filter((_, i) => i !== index))">删除指导</button>
    </fieldset>
  </section>
</template>

<style scoped>
.guidance-entry { border: 1px solid var(--border, #ddd); border-radius: 8px; margin: 10px 0; padding: 12px; display: grid; gap: 8px; }
.guidance-entry textarea { min-height: 100px; }
</style>
