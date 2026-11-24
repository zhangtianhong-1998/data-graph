<script setup lang="ts">
import { ref } from 'vue'
defineProps<{ title: string }>()
const opened = ref(false)
function toggle(event: Event) { opened.value = (event.currentTarget as HTMLDetailsElement).open }
</script>

<template>
  <details @toggle.self="toggle">
    <summary>{{ title }}</summary>
    <slot v-if="opened" />
  </details>
</template>
