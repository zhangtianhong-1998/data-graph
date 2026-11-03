<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { projectPath, request } from './api'
import { isActive } from './state'
import type { Run } from './types'
import ConfirmDelete from './ConfirmDelete.vue'

const props = defineProps<{ run: Run; configRevision?: string }>()
type TraceEvent = { seq: number; time: string; kind: string; data: unknown }
type TracePage = { events: TraceEvent[]; next_offset: number | null; total_events: number; status: 'available' | 'not_recorded' | 'cleared'; active: boolean; incomplete?: boolean; error_code?: string | null }
const page = ref<TracePage | null>(null), loading = ref(false), error = ref('')
const offset = ref(0), previous = ref<number[]>([]), expanded = ref(new Set<number>())
const confirming = ref(false), deleting = ref(false), deleteError = ref('')
const path = computed(() => `${projectPath(props.run.project_id)}/runs/${encodeURIComponent(props.run.run_id)}/trace`)
const active = computed(() => isActive(props.run) || page.value?.active === true)
const labels: Record<string, string> = {
  run_started: '运行开始', run_finished: '运行结束', run_completed: '运行结束',
  model_request: '模型请求', model_response: '模型响应', model_error: '模型请求失败',
  tool_request: '工具输入', tool_call: '工具调用', tool_result: '工具结果', tool_error: '工具失败',
  agent_started: 'Agent 开始', agent_completed: 'Agent 结束', agent_finished: 'Agent 结束',
  proposal_submitted: '提交提案', proposal_result: '提案结果',
  run_configuration: '本次运行配置', pipeline_started: '抽取流程开始', progress: '阶段进展',
  agent_start: 'Agent 开始', context_window: '上下文组装', model_wire_response: '模型接口原始响应',
  agent_end: 'Agent 结束', pipeline_completed: '抽取流程结束', pipeline_error: '抽取流程异常',
  discovery_phase_feedback: '提交阶段工具纠正',
}
let epoch = 0, controller: AbortController | undefined, timer: ReturnType<typeof setTimeout> | undefined
function schedule() {
  clearTimeout(timer)
  if (isActive(props.run)) timer = setTimeout(() => { void load() }, 3000)
}
async function load(next = offset.value) {
  const requestEpoch = ++epoch
  clearTimeout(timer); controller?.abort(); controller = new AbortController()
  loading.value = true; error.value = ''
  try {
    const value = await request<TracePage>(`${path.value}?offset=${next}&limit=10`, { signal: controller.signal })
    if (requestEpoch !== epoch) return
    page.value = value; offset.value = next
  } catch (exception) {
    if (requestEpoch === epoch) error.value = exception instanceof Error ? exception.message : '运行轨迹读取失败。'
  } finally {
    if (requestEpoch === epoch) { loading.value = false; schedule() }
  }
}
function nextPage() {
  if (page.value?.next_offset == null) return
  previous.value.push(offset.value); expanded.value.clear(); void load(page.value.next_offset)
}
function previousPage() { const value = previous.value.pop(); if (value != null) { expanded.value.clear(); void load(value) } }
function toggle(seq: number, event: Event) {
  if ((event.currentTarget as HTMLDetailsElement).open) expanded.value.add(seq)
  else expanded.value.delete(seq)
}
async function clearTrace() {
  if (active.value || deleting.value || !props.configRevision) return
  deleting.value = true; deleteError.value = ''
  try {
    await request(path.value, { method: 'DELETE', headers: { 'If-Match': props.configRevision } })
    confirming.value = false; previous.value = []; expanded.value.clear(); await load(0)
  } catch (exception) { deleteError.value = exception instanceof Error ? exception.message : '轨迹清理失败。' }
  finally { deleting.value = false }
}
function time(value: string) { const date = new Date(value); return Number.isFinite(date.getTime()) ? date.toLocaleString('zh-CN', { hour12: false }) : '—' }
watch(() => [props.run.project_id, props.run.run_id], () => {
  page.value = null; offset.value = 0; previous.value = []; expanded.value.clear(); confirming.value = false; deleteError.value = ''; void load(0)
}, { immediate: true })
watch(() => props.run.status, () => { void load() })
onUnmounted(() => { epoch++; clearTimeout(timer); controller?.abort() })
</script>

<template>
  <section class="panel run-trace" aria-label="运行轨迹">
    <div class="panel-head"><h2>运行轨迹</h2><span class="badge">{{ page?.total_events ?? 0 }} 步</span><div class="actions"><button class="btn small" :disabled="loading || deleting" @click="load()">刷新轨迹</button><a v-if="page?.status === 'available'" class="btn small" :href="`/api${path}/export`" :download="`${run.run_id}-trace.jsonl`">{{ page.incomplete || active ? '导出已记录轨迹' : '导出完整轨迹' }}</a><button class="btn small danger" :disabled="active || deleting || !configRevision || page?.status !== 'available'" @click="confirming = true; deleteError = ''">清理轨迹</button></div></div>
    <div class="panel-body">
      <p class="field-hint">按顺序保存模型实际输入、返回内容和工具执行过程。轨迹可能包含样本数据；凭据会脱敏，运行结束后保留，由你主动清理。</p>
      <p v-if="active" class="field-hint">运行中每 3 秒刷新当前页。导出包含截至下载时已写入的记录；运行结束后可导出完整轨迹。运行中不能清理。</p>
      <p v-if="page?.incomplete" class="note amber" role="alert">运行轨迹不完整：{{ page.error_code === 'trace_write_failed' ? '部分事件写入失败' : '部分记录缺失或无法读取' }}。可导出已记录的部分，不能据此还原全部执行过程。</p>
      <p v-if="error" class="note amber" role="alert">{{ error }}</p>
      <p v-if="loading && !page" role="status">正在读取轨迹…</p>
      <p v-else-if="page?.status === 'not_recorded' && !page.incomplete" class="note">此运行没有记录完整轨迹，历史输入无法从汇总报告中恢复。</p>
      <p v-else-if="page?.status === 'cleared'" class="note">此运行的轨迹已手动清理，运行报告和图谱结果仍保留。</p>
      <p v-else-if="!page?.events.length && !error" class="note">尚无轨迹事件。</p>
      <details v-for="event in page?.events || []" :key="`${run.run_id}:${event.seq}`" class="trace-step" @toggle="toggle(event.seq, $event)">
        <summary><span class="badge">第 {{ event.seq }} 步</span><strong>{{ labels[event.kind] || event.kind }}</strong><time>{{ time(event.time) }}</time></summary>
        <pre v-if="expanded.has(event.seq)" class="code trace-payload">{{ JSON.stringify(event.data, null, 2) }}</pre>
      </details>
    </div>
    <div v-if="page?.status === 'available'" class="panel-footer"><span>当前显示 {{ page.events.length ? offset + 1 : 0 }}–{{ offset + page.events.length }} / {{ page.total_events }} 步；导出不受分页限制</span><div class="actions"><button class="btn small" :disabled="!previous.length || loading" @click="previousPage">上一页轨迹</button><button class="btn small" :disabled="page.next_offset == null || loading" @click="nextPage">下一页轨迹</button></div></div>
  </section>
  <ConfirmDelete v-if="confirming" title="清理运行轨迹" description="删除本次运行保存的模型输入、输出和工具执行轨迹？此操作无法撤销。" detail="运行报告、已发布图谱和原始上传文件会保留。需要留档时，请先导出完整轨迹。" :busy="deleting" :locked="deleting || active || !configRevision" :error="deleteError" @cancel="confirming = false" @confirm="clearTrace"/>
</template>

<style scoped>
.run-trace { margin-top: 16px; }
.run-trace .panel-head { flex-wrap: wrap; gap: 10px; }
.run-trace .panel-head .actions { margin-left: auto; }
.trace-step { border-top: 1px solid var(--line, #e5eaf1); }
.trace-step summary { cursor: pointer; display: flex; align-items: center; gap: 10px; padding: 12px 0; }
.trace-step time { margin-left: auto; color: #718096; font-size: 12px; }
.trace-payload { max-height: 480px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
