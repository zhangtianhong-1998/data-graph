<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { isActive, runLabels, stageLabel } from './state'
import type { Run, RunEvent } from './types'

const props = defineProps<{ run: Run; refreshError?: boolean }>()
const active = computed(() => isActive(props.run))
const snapshot = computed(() => props.run.progress)
const stage = computed(() => snapshot.value?.stage || props.run.stage)
const follow = ref(true)
const log = ref<HTMLElement | null>(null)
const now = ref(Date.now())
let timer: ReturnType<typeof setInterval> | undefined
watch(active, value => {
  clearInterval(timer)
  now.value = Date.now()
  if (value) timer = setInterval(() => { now.value = Date.now() }, 1000)
}, { immediate: true })
onUnmounted(() => clearInterval(timer))

const events = computed(() => (snapshot.value?.events || []).slice(-200))
const omitted = computed(() => (snapshot.value?.dropped_events || 0) + Math.max(0, (snapshot.value?.events.length || 0) - 200))
const units: Record<string, string> = { tables: '张表', rules: '条规则', documents: '份文档' }
const counts = computed(() => {
  const p = snapshot.value
  if (!p || typeof p.completed !== 'number' || typeof p.total !== 'number' ||
      !Number.isSafeInteger(p.completed) || !Number.isSafeInteger(p.total) || p.total <= 0 || p.completed < 0 || p.completed > p.total) return null
  return { completed: p.completed, total: p.total, percent: Math.floor(p.completed / p.total * 100), unit: units[p.unit || ''] || '项' }
})
const progressText = computed(() => {
  if (!active.value) return runLabels[props.run.status]
  if (counts.value) return `${counts.value.completed} / ${counts.value.total} ${counts.value.unit} · ${counts.value.percent}%`
  return snapshot.value ? '正在处理，当前阶段未提供总量' : '等待后端上报进度'
})
const elapsed = computed(() => {
  const start = Date.parse(props.run.created_at)
  const end = active.value ? now.value : Date.parse(props.run.finished_at || '')
  const seconds = Number.isFinite(start) && Number.isFinite(end) ? Math.max(0, Math.floor((end - start) / 1000)) : Math.max(0, Math.floor(snapshot.value?.elapsed_seconds || 0))
  const h = Math.floor(seconds / 3600), m = Math.floor(seconds % 3600 / 60), s = seconds % 60
  return h ? `${h} 小时 ${m} 分 ${s} 秒` : m ? `${m} 分 ${s} 秒` : `${s} 秒`
})
const counterLabels = [
  ['model_calls', '模型调用'], ['input_tokens', '输入 tokens'], ['output_tokens', '输出 tokens'],
  ['published_rules', '已发布规则'], ['db_queries', '数据库查询'],
] as const
const counters = computed(() => counterLabels.flatMap(([key, label]) => {
  const value = snapshot.value?.counters[key]
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? [{ key, label, value: value.toLocaleString('zh-CN') }] : []
}))
const labels: Record<string, string> = {
  run_started: '抽取已启动', run_completed: '抽取已完成', run_completed_with_gaps: '抽取已结束，仍有未决项',
  run_partial: '抽取未全部完成', run_failed: '抽取失败', run_cancelled: '抽取已取消', run_interrupted: '抽取中断',
  stage_started: '开始阶段', stage_completed: '阶段处理结束',
  source_open_started: '开始连接数据来源', source_open_completed: '数据来源已就绪',
  table_started: '开始处理表', table_completed: '表处理结束',
  rule_started: '开始处理规则', rule_completed: '规则处理结束', rule_rejected: '规则未通过验证',
  model_call_started: '已发起模型调用，等待返回', model_call_completed: '模型调用已返回', model_call_failed: '模型调用失败',
  publication_started: '开始发布', publication_completed: '发布完成',
  cleanup_started: '开始清理临时数据', cleanup_completed: '临时数据清理结束', issue_detected: '发现需要处理的问题',
}
const levelLabels: Record<string, string> = { info: '信息', warning: '注意', error: '错误' }
const eventLabel = (event: RunEvent) => labels[event.code] || '运行事件'
function time(value: string): string {
  const date = new Date(value)
  return Number.isFinite(date.getTime()) ? date.toLocaleTimeString('zh-CN', { hour12: false }) : '—'
}
function eventCounts(event: RunEvent): string {
  if (typeof event.completed !== 'number' || typeof event.total !== 'number' || event.total < 0) return ''
  return `${event.completed} / ${event.total} ${units[event.unit || ''] || '项'}`
}
async function toBottom() {
  await nextTick()
  if (follow.value && log.value) log.value.scrollTop = log.value.scrollHeight
}
function onScroll() {
  if (log.value && log.value.scrollHeight - log.value.clientHeight - log.value.scrollTop > 24) follow.value = false
}
function toggleFollow() {
  follow.value = !follow.value
  if (follow.value) void toBottom()
}
watch(() => [props.run.run_id, props.run.project_id], () => { follow.value = true; void toBottom() })
watch(() => events.value.at(-1)?.seq, () => { if (follow.value) void toBottom() }, { immediate: true })
</script>

<template>
  <section class="panel runtime-panel" aria-label="运行进度与日志">
    <div class="panel-head runtime-heading"><div><h2>运行进度与日志</h2><p class="runtime-id">{{ run.run_id }}</p></div><span class="badge" :class="active ? 'blue' : run.status === 'completed' ? 'green' : 'amber'">{{ runLabels[run.status] }}</span></div>
    <div class="panel-body runtime-progress">
      <div class="runtime-progress-title"><div><small>当前阶段进度</small><strong>{{ stageLabel(stage) }}</strong></div><span class="runtime-progress-value">{{ progressText }}</span></div>
      <div v-if="active" class="runtime-track" :class="{ indeterminate: !counts, stale: refreshError }" role="progressbar" aria-label="当前阶段进度" :aria-valuenow="counts?.percent" :aria-valuemin="0" :aria-valuemax="100" :aria-valuetext="refreshError ? `刷新失败；上次记录：${progressText}` : progressText"><span :style="counts ? { width: `${counts.percent}%` } : undefined"></span></div>
      <p v-if="active && counts" class="runtime-hint">这是当前阶段的任务完成比例，不代表全流程或图谱覆盖率。</p>
      <p v-else-if="!active && run.status === 'completed_with_gaps'" class="runtime-hint">执行流程已结束；未决项与已发布结果请查看下方报告。</p>
      <p v-if="refreshError" class="runtime-refresh-error" role="status">进度刷新失败，当前显示上次收到的记录。正在重试。</p>
      <div class="runtime-metrics"><div><small>{{ active ? '已用时' : '运行用时' }}</small><strong>{{ elapsed }}</strong></div><div v-for="counter in counters" :key="counter.key"><small>{{ counter.label }}</small><strong>{{ counter.value }}</strong></div></div>
    </div>
    <div class="runtime-log-head"><div><h3>运行日志</h3><p>{{ active ? '每秒获取最新状态' : '本次运行的过程记录' }}<template v-if="snapshot?.updated_at"> · 最近事件 {{ time(snapshot.updated_at) }}</template></p></div><button v-if="events.length" class="btn small" :aria-pressed="follow" @click="toggleFollow">{{ follow ? '暂停跟随' : '继续跟随' }}</button></div>
    <p v-if="omitted" class="runtime-log-notice">仅保留最近 {{ events.length }} 条日志，较早的 {{ omitted }} 条已省略。</p>
    <div v-if="events.length" ref="log" class="runtime-log" role="log" aria-label="运行日志" :aria-live="follow ? 'polite' : 'off'" aria-relevant="additions" tabindex="0" @scroll="onScroll">
      <div v-for="event in events" :key="event.seq" class="runtime-log-row" :class="{ 'log-warning': event.level === 'warning', 'log-error': event.level === 'error' }">
        <time :datetime="event.time">{{ time(event.time) }}</time><span class="runtime-log-level">{{ levelLabels[event.level] || '信息' }}</span><div class="runtime-log-message"><span>{{ eventLabel(event) }}</span><small>{{ stageLabel(event.stage) }}<template v-if="event.table"> · {{ event.table }}</template><template v-if="event.rule_id"> · {{ event.rule_id }}</template><template v-if="eventCounts(event)"> · {{ eventCounts(event) }}</template><template v-if="event.code.startsWith('model_call_') && event.counters?.model_calls != null"> · 第 {{ event.counters.model_calls }} 次调用</template></small></div>
      </div>
    </div>
    <p v-else class="runtime-log-empty">{{ snapshot ? '暂无运行事件，等待后端上报。' : '此运行尚未提供过程记录。旧版本启动的运行无法补回实时日志。' }}</p>
    <p class="runtime-log-footnote">日志记录执行阶段与计数，不包含模型原文、数据行或密钥。</p>
  </section>
</template>

<style scoped>
.runtime-panel{margin-bottom:20px;overflow:hidden}.runtime-heading{gap:14px}.runtime-id{color:var(--muted);font:11px ui-monospace,monospace;overflow-wrap:anywhere;margin-top:7px}.runtime-progress-title{display:flex;align-items:flex-end;justify-content:space-between;gap:16px}.runtime-progress-title small{display:block;font-size:11px;color:var(--muted);margin-bottom:8px}.runtime-progress-title strong{font-size:17px;font-weight:600}.runtime-progress-value{font-size:12px;color:#536887;text-align:right}.runtime-track{height:9px;background:#eaf0f8;border-radius:20px;overflow:hidden;margin-top:17px}.runtime-track>span{display:block;height:100%;border-radius:inherit;background:var(--blue);transition:width .2s}.runtime-track.indeterminate>span{width:28%;animation:runtime-wait 1.8s ease-in-out infinite}.runtime-track.stale>span{animation-play-state:paused;background:#b68a3d}.runtime-hint{font-size:11px;color:var(--muted);margin-top:10px}.runtime-refresh-error{font-size:12px;color:#8e6219;background:#fff6e7;border:1px solid #eddbb6;border-radius:6px;padding:9px 12px;margin-top:12px}.runtime-metrics{display:flex;flex-wrap:wrap;gap:18px 32px;margin-top:20px}.runtime-metrics small{display:block;font-size:10px;color:var(--muted);margin-bottom:6px}.runtime-metrics strong{font-size:13px;font-weight:600;font-variant-numeric:tabular-nums}.runtime-log-head{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:16px 24px;border-top:1px solid var(--line);background:#fafbfd}.runtime-log-head p{font-size:10px;color:var(--muted);margin-top:5px}.runtime-log-notice{padding:8px 24px;color:#8a6e3d;background:#fffaf0;font-size:11px;border-top:1px solid #f0e7d5}.runtime-log{max-height:320px;min-height:120px;overflow:auto;overscroll-behavior:contain;padding:6px 24px;background:#fff}.runtime-log:focus-visible{outline:2px solid #a6c1ff;outline-offset:-3px}.runtime-log-row{display:grid;grid-template-columns:64px 30px minmax(0,1fr);gap:12px;padding:12px 0;border-bottom:1px solid #f0f3f8;align-items:start;font-size:12px}.runtime-log-row:last-child{border-bottom:0}.runtime-log-row time{font:11px ui-monospace,monospace;color:#8290a5;padding-top:2px}.runtime-log-level{font-size:10px;color:#6e8099;padding-top:2px}.log-warning .runtime-log-level{color:#9e701c}.log-error .runtime-log-level{color:#b83e50}.runtime-log-message{overflow-wrap:anywhere;line-height:1.6}.runtime-log-message small{display:block;color:#8390a4;font-size:10px;margin-top:3px}.runtime-log-empty{padding:25px 24px;color:var(--muted);font-size:12px}.runtime-log-footnote{padding:12px 24px;border-top:1px solid var(--line);color:var(--muted);font-size:10px;background:#fafbfd}@keyframes runtime-wait{0%{transform:translateX(-100%)}100%{transform:translateX(360%)}}@media(max-width:740px){.runtime-progress-title{align-items:flex-start;flex-direction:column;gap:9px}.runtime-progress-value{text-align:left}.runtime-metrics{gap:16px 22px}.runtime-log-head,.runtime-log,.runtime-log-notice,.runtime-log-footnote{padding-left:16px;padding-right:16px}.runtime-log-row{grid-template-columns:56px 26px minmax(0,1fr);gap:7px}}@media(prefers-reduced-motion:reduce){.runtime-track>span{transition:none}.runtime-track.indeterminate>span{animation:none;width:100%;background:repeating-linear-gradient(90deg,#c2d6fa 0 12px,#eaf0f8 12px 24px)}}
</style>
