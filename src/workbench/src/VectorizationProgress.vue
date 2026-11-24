<script setup lang="ts">
import { computed } from 'vue'
const props = defineProps<{ progress?: { stage: string; completed: number; total: number | null } }>()
const percent = computed(() => props.progress?.stage === 'embedding' && props.progress.total
  ? Math.floor(100 * props.progress.completed / props.progress.total) : undefined)
</script>
<template>
  <section class="vector-build" aria-label="索引进度">
    <div role="status">
      <strong>{{ progress?.stage === 'embedding' ? '生成文本向量' : progress?.stage === 'publishing' ? '写入检索索引' : '准备检索数据' }}</strong>
      <span v-if="percent !== undefined && progress">{{ progress.completed }} / {{ progress.total }} 条文本 · {{ percent }}%</span>
    </div>
    <progress max="100" :value="percent" aria-label="当前阶段进度" />
  </section>
</template>
<style scoped>
.vector-build { padding: .8rem 1rem; border: 1px solid #d7dfec; border-radius: .65rem; background: white; margin-bottom: .75rem; }
.vector-build div { display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: .5rem; font-size: .85rem; }
.vector-build span { font-variant-numeric: tabular-nums; color: #465978; }
progress { display: block; width: 100%; height: .4rem; margin-top: .55rem; accent-color: #3766ba; }
</style>
