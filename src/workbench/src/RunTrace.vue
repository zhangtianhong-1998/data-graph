<script setup lang="ts">
import { computed, onUnmounted, ref, shallowRef, watch } from 'vue'
import { projectPath, request } from './api'
import { isActive } from './state'
import type { Run } from './types'
import ConfirmDelete from './ConfirmDelete.vue'
import TracePayload from './TracePayload.vue'
import WorkerDiagnostics from './WorkerDiagnostics.vue'
import { requireNodeTrace, traceContext, traceToolName } from './tracePresentation'
import type { NodeTraceIdentity, TraceEvent } from './tracePresentation'

const props = defineProps<{ run: Run; configRevision?: string; nodeTrace?: { nodeId: string; runId: string; status: string } }>()
const emit = defineEmits<{ close: [] }>()
type TracePage = Partial<NodeTraceIdentity> & { events: TraceEvent[]; next_offset: number | null; total_events: number; recorded_events?: number; status: 'available' | 'not_recorded' | 'cleared'; active: boolean; incomplete?: boolean; error_code?: string | null; reused?: boolean }
const opened = ref(Boolean(props.nodeTrace))
const page = shallowRef<TracePage | null>(null), loading = ref(false), error = ref('')
const view = ref<'agent' | 'runtime' | 'all'>('agent')
const offset = ref(0), previous = ref<number[]>([]), expanded = ref(new Set<number>())
const confirming = ref(false), deleting = ref(false), deleteError = ref('')
const identity = computed(() => props.nodeTrace && {
  parent_run_id: props.run.run_id, node_id: props.nodeTrace.nodeId, run_id: props.nodeTrace.runId,
})
const path = computed(() => {
  const base = `${projectPath(props.run.project_id)}/runs/${encodeURIComponent(props.run.run_id)}`
  return props.nodeTrace ? `${base}/nodes/${encodeURIComponent(props.nodeTrace.nodeId)}/trace` : `${base}/trace`
})
const running = computed(() => {
  if (!isActive(props.run)) return false
  if (!props.nodeTrace) return true
  return !page.value?.reused && (page.value?.active ?? ['queued', 'running'].includes(props.nodeTrace.status))
})
const active = computed(() => running.value || (!props.nodeTrace && page.value?.active === true))
const labels: Record<string, string> = {
  run_started: '运行开始', run_finished: '运行结束', run_completed: '运行结束',
  model_request: '模型请求', model_response: '模型响应', model_error: '模型请求失败',
  model_request_completed: '请求完成计时', model_transport_config: '接口连接设置',
  tool_request: '工具输入', tool_call: '工具调用', tool_result: '工具结果', tool_error: '工具失败',
  agent_started: 'Agent 开始', agent_completed: 'Agent 结束', agent_finished: 'Agent 结束',
  proposal_submitted: '提交提案', proposal_result: '提案结果',
  run_configuration: '本次运行配置', pipeline_started: '抽取流程开始', progress: '阶段进展',
  worker_diagnostic: '进程诊断',
  agent_start: 'Agent 开始', context_window: '上下文组装', context_compaction: '上下文压缩', model_wire_response: '模型接口原始响应',
  context_history_edit: '历史结果压缩',
  agent_end: 'Agent 结束', pipeline_completed: '抽取流程结束', pipeline_error: '抽取流程异常',
  discovery_phase_feedback: '提交阶段工具纠正',
  model_retry_skipped: '模型重试跳过', discovery_time_submission: '按剩余时间进入提交',
  discovery_timeout_submission: '超时后提交已有发现',
  request_context_deduplicated: '复用重复上下文',
}
let epoch = 0, controller: AbortController | undefined, timer: ReturnType<typeof setTimeout> | undefined
function schedule() {
  clearTimeout(timer)
  if (opened.value && running.value) timer = setTimeout(() => { void load() }, 3000)
}
async function load(next = offset.value, movement?: 'next' | 'previous') {
  if (!opened.value) return
  const requestEpoch = ++epoch
  clearTimeout(timer); controller?.abort(); controller = new AbortController()
  loading.value = true; error.value = ''
  try {
    const value = await request<TracePage>(`${path.value}?offset=${next}&limit=10&view=${view.value}&summary=true`, { signal: controller.signal })
    if (requestEpoch !== epoch) return
    requireNodeTrace(value, identity.value)
    if (movement === 'next') previous.value.push(offset.value)
    if (movement === 'previous') previous.value.pop()
    if (movement) expanded.value.clear()
    page.value = value; offset.value = next
  } catch (exception) {
    if (requestEpoch === epoch) {
      if (props.nodeTrace) { page.value = null; expanded.value.clear() }
      error.value = exception instanceof Error ? exception.message : '运行轨迹读取失败。'
    }
  } finally {
    if (requestEpoch === epoch) { loading.value = false; schedule() }
  }
}
function nextPage() {
  if (page.value?.next_offset == null) return
  void load(page.value.next_offset, 'next')
}
function previousPage() { const value = previous.value.at(-1); if (value != null) void load(value, 'previous') }
function toggle(seq: number, event: Event) {
  if ((event.currentTarget as HTMLDetailsElement).open) expanded.value.add(seq)
  else expanded.value.delete(seq)
}
async function clearTrace() {
  if (props.nodeTrace || active.value || deleting.value || !props.configRevision) return
  const target = path.value
  deleting.value = true; deleteError.value = ''
  try {
    await request(target, { method: 'DELETE', headers: { 'If-Match': props.configRevision } })
    if (target !== path.value || !opened.value) return
    confirming.value = false; previous.value = []; expanded.value.clear(); await load(0)
  } catch (exception) { if (target === path.value) deleteError.value = exception instanceof Error ? exception.message : '轨迹清理失败。' }
  finally { deleting.value = false }
}
function time(value: string) { const date = new Date(value); return Number.isFinite(date.getTime()) ? date.toLocaleString('zh-CN', { hour12: false }) : '—' }
function resetPage() {
  epoch++; controller?.abort(); clearTimeout(timer); loading.value = false; error.value = ''
  page.value = null; offset.value = 0; previous.value = []; expanded.value.clear(); confirming.value = false; deleteError.value = ''
}
watch([opened, path, view], () => { resetPage(); if (opened.value) void load(0) }, { immediate: true })
watch([() => props.nodeTrace?.status, () => props.run.status, path], ([node, parent, currentPath], [oldNode, oldParent, previousPath]) => {
  if (opened.value && currentPath === previousPath && (node !== oldNode || parent !== oldParent)) void load()
})
onUnmounted(() => { opened.value = false; epoch++; clearTimeout(timer); controller?.abort() })
</script>

<template>
  <section class="panel run-trace" :aria-label="nodeTrace ? '节点轨迹' : '运行轨迹'">
    <div class="panel-head">
      <h2>{{ nodeTrace ? `节点轨迹 · ${nodeTrace.nodeId}` : '运行轨迹' }}</h2>
      <span v-if="page" class="badge">{{ page.total_events }} 条记录</span>
      <div class="actions">
        <button v-if="nodeTrace" class="btn small" @click="emit('close')">关闭节点轨迹</button>
        <button v-else class="btn small" :aria-expanded="opened" @click="opened = !opened">{{ opened ? '收起轨迹' : '查看运行轨迹' }}</button>
        <template v-if="opened">
          <button class="btn small" :disabled="loading || deleting" @click="load()">刷新轨迹</button>
          <a v-if="!nodeTrace && page?.status === 'available'" class="btn small" :href="`/api${path}/export`" :download="`${run.run_id}-trace.jsonl`">{{ page.incomplete || active ? '导出已记录轨迹' : '导出完整轨迹' }}</a>
          <button v-if="!nodeTrace" class="btn small danger" :disabled="active || deleting || !configRevision || page?.status !== 'available'" @click="confirming = true; deleteError = ''">清理轨迹</button>
        </template>
      </div>
    </div>
    <div v-if="opened" class="panel-body">
      <p v-if="nodeTrace" class="field-hint">父运行 <span class="mono">{{ run.run_id }}</span> · {{ page?.reused ? '本次复用的子运行' : '子运行' }} <span class="mono">{{ nodeTrace.runId }}</span></p>
      <div class="trace-filters"><label>显示内容 <select v-model="view" aria-label="轨迹显示内容"><option value="agent">Agent 调用</option><option value="runtime">运行与接口事件</option><option value="all">全部原始记录</option></select></label><span class="field-hint">原始记录共 {{ page?.recorded_events ?? page?.total_events ?? 0 }} 条</span></div>
      <template v-if="!nodeTrace">
        <p class="field-hint">Agent 调用按实际顺序显示模型输入、模型返回、工具参数与结果。阶段进展、接口原始数据和计时保留在“运行与接口事件”中；完整导出包含所有记录。记录序号不是探索轮数，也不代表模型未返回的内部思考。</p>
        <p class="field-hint">轨迹可能包含样本数据；凭据会脱敏，运行结束后保留，由你主动清理。</p>
      </template>
      <p v-if="active" class="field-hint">运行中每 3 秒刷新当前页。<template v-if="!nodeTrace">导出包含截至下载时已写入的记录；运行结束后可导出完整轨迹。运行中不能清理。</template></p>
      <p v-if="page?.incomplete" class="note amber" role="alert">运行轨迹不完整：{{ page.error_code === 'trace_write_failed' ? '部分事件写入失败' : '部分记录缺失或无法读取' }}。<template v-if="!nodeTrace">可导出已记录的部分，不能据此还原全部执行过程。</template></p>
      <p v-if="error" class="note amber" role="alert">{{ error }}</p>
      <p v-if="loading && !page" role="status">正在读取轨迹…</p>
      <p v-else-if="page?.status === 'not_recorded' && !page.incomplete" class="note">此运行没有记录完整轨迹，历史输入无法从汇总报告中恢复。</p>
      <p v-else-if="page?.status === 'cleared'" class="note">此运行的轨迹已手动清理，运行报告和图谱结果仍保留。</p>
      <p v-else-if="!page?.events.length && !error" class="note">当前分类尚无记录；可切换到“全部原始记录”查看已保存内容。</p>
      <details v-for="(event, index) in page?.events || []" :key="`${run.run_id}:${event.seq}`" class="trace-step" :open="expanded.has(event.seq)" @toggle="toggle(event.seq, $event)">
        <summary><span class="badge">记录 #{{ event.seq }}</span><div class="trace-summary"><strong>{{ labels[event.kind] || event.kind }}<span v-if="traceToolName(event)"> · {{ traceToolName(event) }}</span></strong><span v-if="traceContext(event)" class="field-hint">{{ traceContext(event) }}</span></div><time>{{ time(event.time) }}</time></summary>
        <TracePayload v-if="expanded.has(event.seq)" :seq="event.seq" :identity="identity" :url="`${path}?offset=${offset + index}&limit=1&view=${view}`"/>
      </details>
    </div>
    <div v-if="page?.status === 'available'" class="panel-footer"><span>当前显示 {{ page.events.length ? offset + 1 : 0 }}–{{ offset + page.events.length }} / {{ page.total_events }} 条<template v-if="!nodeTrace">；导出不受筛选与分页限制</template></span><div class="actions"><button class="btn small" :disabled="!previous.length || loading" @click="previousPage">上一页轨迹</button><button class="btn small" :disabled="page.next_offset == null || loading" @click="nextPage">下一页轨迹</button></div></div>
    <WorkerDiagnostics v-if="!nodeTrace" class="panel-body" :key="`${run.project_id}:${run.run_id}:${page?.status === 'cleared'}`" :run="run"/>
  </section>
  <ConfirmDelete v-if="confirming" title="清理运行轨迹" description="删除本次运行保存的模型输入、输出、工具执行轨迹和进程诊断？此操作无法撤销。" detail="运行报告、已发布图谱和原始上传文件会保留。需要留档时，请先导出完整轨迹和进程诊断。" :busy="deleting" :locked="deleting || active || !configRevision" :error="deleteError" @cancel="confirming = false" @confirm="clearTrace"/>
</template>

<style scoped>
.run-trace { margin-top: 16px; }
.run-trace .panel-head { flex-wrap: wrap; gap: 10px; }
.run-trace .panel-head .actions { margin-left: auto; }
.trace-filters { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; margin-bottom: 10px; }
.trace-filters select { margin-left: 8px; }
.trace-summary { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.trace-step { border-top: 1px solid var(--line, #e5eaf1); }
.trace-step summary { cursor: pointer; display: flex; align-items: center; gap: 10px; padding: 12px 0; }
.trace-step time { margin-left: auto; color: #718096; font-size: 12px; }
.trace-payload { max-height: 480px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
