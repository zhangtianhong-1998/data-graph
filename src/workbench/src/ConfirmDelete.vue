<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref } from 'vue'
const props = defineProps<{ title: string; description: string; detail?: string; busy: boolean; locked: boolean; error: string }>()
const emit = defineEmits<{ confirm: []; cancel: [] }>()
const panel = ref<HTMLElement | null>(null), cancel = ref<HTMLButtonElement | null>(null)
let previous: HTMLElement | null = null
function keydown(event: KeyboardEvent) {
  if (event.key === 'Escape') { event.preventDefault(); if (!props.busy) emit('cancel') }
  if (event.key !== 'Tab') return
  const controls = [...(panel.value?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') || [])]
  if (!controls.length) { event.preventDefault(); return }
  const first = controls[0], last = controls[controls.length - 1]
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
}
onMounted(async () => { previous = document.activeElement as HTMLElement | null; await nextTick(); cancel.value?.focus() })
onUnmounted(() => { if (previous?.isConnected) previous.focus() })
</script>
<template>
  <div class="modal-backdrop" @click.self="!busy && emit('cancel')" @keydown="keydown">
    <section ref="panel" class="modal" role="alertdialog" aria-modal="true" aria-labelledby="delete-title" aria-describedby="delete-description">
      <div class="panel-head"><h2 id="delete-title">{{ title }}</h2></div>
      <div class="panel-body"><p id="delete-description" class="delete-description">{{ description }}</p><p v-if="detail" class="field-hint">{{ detail }}</p><p v-if="error" class="note amber" role="alert">{{ error }}</p></div>
      <div class="panel-footer"><button ref="cancel" class="btn" :disabled="busy" @click="emit('cancel')">取消</button><button class="btn danger" :disabled="locked" @click="emit('confirm')">{{ busy ? '正在删除…' : '确认删除' }}</button></div>
    </section>
  </div>
</template>
