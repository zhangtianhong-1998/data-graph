<script setup lang="ts">
import type { FusionPolicy } from './types'
defineProps<{ modelValue: FusionPolicy; locked: boolean; title?: string }>()
</script>
<template>
  <section class="panel fusion-summary">
    <p>合并时保留来源、事实的适用范围和不同推理路径。身份或定义尚未对齐的对象分别保留，不阻断其他成果。</p>
    <p v-if="modelValue.entity_rules.length || modelValue.label_conflict_policy !== 'retain_variants'" class="field-hint">此项目保留了已有自定义策略；本次编辑不会覆盖它们。</p>
  </section>
</template>
<style scoped>.fusion-summary { padding: 12px 16px; margin-bottom: 14px }</style>
