<script setup lang="ts" generic="T">
import { computed, ref, watch } from 'vue'

const props = defineProps<{ items: readonly T[]; label: string }>()
const pageSize = 50
const page = ref(0)
const pages = computed(() => Math.ceil(props.items.length / pageSize))
watch(pages, count => { page.value = Math.min(page.value, Math.max(0, count - 1)) }, { flush: 'sync' })
const start = computed(() => page.value * pageSize)
const visible = computed(() => props.items.slice(start.value, start.value + pageSize))
</script>

<template>
  <slot :items="visible" />
  <nav v-if="pages > 1" class="actions" :aria-label="label + '分页'">
    <span aria-live="polite">{{ label }} {{ start + 1 }}–{{ start + visible.length }} / {{ items.length }}</span>
    <button class="btn small" type="button" :disabled="page === 0" @click="page--">上一页</button>
    <button class="btn small" type="button" :disabled="page + 1 >= pages" @click="page++">下一页</button>
  </nav>
</template>
