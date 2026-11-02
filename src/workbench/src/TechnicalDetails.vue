<script setup lang="ts">
import { ref } from 'vue'
const props = defineProps<{ value: unknown; title?: string }>()
const copied = ref('')
async function copy() {
  try { await navigator.clipboard.writeText(JSON.stringify(props.value, null, 2)); copied.value = '已复制' }
  catch { copied.value = '复制失败，可选中文本手动复制' }
}
</script>
<template><details class="technical-details"><summary>{{ title || '技术标识与原始结构' }}</summary><button class="btn small" @click="copy">复制</button> <span role="status">{{ copied }}</span><pre class="code detail-code">{{ JSON.stringify(value, null, 2) }}</pre></details></template>
<style scoped>.technical-details{margin:10px 0;font-size:12px}.technical-details summary{cursor:pointer;color:var(--muted);margin-bottom:6px}</style>
