<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { projectPath, request } from './api'
import { isActive } from './state'
import type { Run } from './types'

const props = defineProps<{ run: Run }>()
type Entry = { time?: string; code: string; stage?: string; error_code?: string; exception_type?: string;
  database?: { stage?: string; category?: string; sqlstate?: string; errno?: number; recovery?: string; connection_closed?: boolean; connection_broken?: boolean;
    probe_code?: string; probe_failure?: string; cause_code?: string; recovery_decision?: string;
    boundary_recovery_attempts?: number; recovery_attempt_limit?: number; terminal?: boolean };
  exit_code?: number; stderr_bytes?: number; connection_generation?: number; reconnect_attempts?: number; frames?: { file: string; line: number }[] }
type Page = { status: 'available' | 'not_recorded' | 'unavailable' | 'cleared'; events: Entry[]; incomplete: boolean; active: boolean }
const opened = ref(false), loading = ref(false), error = ref(''), page = ref<Page | null>(null)
const path = computed(() => `${projectPath(props.run.project_id)}/runs/${encodeURIComponent(props.run.run_id)}/worker-diagnostics`)
const labels: Record<string, string> = {
  worker_starting: '准备启动进程', worker_started: '进程已启动', worker_finished: '进程任务结束',
  worker_error: '进程执行异常', worker_cleanup_error: '资源释放异常', worker_cancel_error: '取消异常',
  worker_process_exited: '进程已退出', worker_stderr_summary: '标准错误流摘要', worker_start_failed: '进程启动失败',
  worker_unhandled_error: '未处理异常', worker_input_invalid: '进程输入无效', worker_log_truncated: '诊断达到容量上限',
  database_diagnostic: '数据库诊断', worker_database_diagnostic: '数据库诊断',
}
const categories: Record<string, string> = {
  unknown: '原因未确定', connection: '连接故障', authentication: '认证失败', permission: '权限拒绝',
  failed_transaction: '事务已中止', interrupted: '请求被中断', idle_transaction_timeout: '空闲事务超时',
  shutdown: '服务端关闭', unavailable: '服务暂不可用', configuration_or_catalog: '配置或目录问题',
  resource: '数据库资源限制', serialization: '事务并发冲突', deadlock: '死锁', lock_timeout: '等待锁超时',
  statement_timeout: '语句超时', unsupported: '功能不支持', lock_unavailable: '锁不可用',
  qualification: '连接能力检查未通过',
}
const probeLabels: Record<string, string> = {
  parameter_binding_unverified: '参数绑定', distinct_projection_unverified: '去重查询',
  read_only_transaction_unverified: '只读事务',
  query_cancel_unverified: '查询取消', statement_timeout_unverified: '语句超时',
  runtime_control_unverified: '查询中断后的会话检查', cte_union_unverified: 'CTE 与 UNION 查询',
}
const probeFailureLabels: Record<string, string> = {
  no_row: '没有返回检查结果', malformed_row: '检查结果格式不符', readback_mismatch: '检查结果与要求不符',
  interrupt_not_observed: '未观察到预期的查询中断', cancel_not_sent: '未能发出取消请求',
  cancel_not_completed: '取消操作未完成',
}
const recoveryDecisionLabels: Record<string, string> = {
  retry_new_session: '丢弃当前会话，重新检查新会话', retry_limit_exhausted: '恢复次数已耗尽',
  qualification_rejected: '资格检查未通过，当前阶段不能重试',
  deterministic_rejection: '配置、权限或目录检查未通过',
  unclassified_failure: '失败原因尚未分类，已保留原始错误并停止',
}
let epoch = 0, controller: AbortController | undefined, timer: ReturnType<typeof setTimeout> | undefined
async function load() {
  const turn = ++epoch
  clearTimeout(timer); controller?.abort(); controller = new AbortController()
  loading.value = true; error.value = ''
  try {
    const result = await request<Page>(path.value, { signal: controller.signal })
    if (turn === epoch) page.value = result
  } catch (exception) {
    if (turn === epoch) error.value = exception instanceof Error ? exception.message : '进程诊断读取失败。'
  } finally {
    if (turn === epoch) {
      loading.value = false
      if (opened.value && isActive(props.run)) timer = setTimeout(() => { void load() }, 5000)
    }
  }
}
function toggle(event: Event) {
  opened.value = (event.currentTarget as HTMLDetailsElement).open
  if (opened.value) void load()
  else { epoch++; controller?.abort(); clearTimeout(timer); loading.value = false }
}
function time(value?: string) { return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—' }
watch([() => props.run.project_id, () => props.run.run_id], () => {
  epoch++; controller?.abort(); clearTimeout(timer); page.value = null; error.value = ''; loading.value = false
  if (opened.value) void load()
})
watch(() => props.run.status, () => { if (opened.value) void load() })
onUnmounted(() => { epoch++; controller?.abort(); clearTimeout(timer) })
</script>

<template>
  <details class="worker-diagnostics" @toggle="toggle">
    <summary>查看进程诊断</summary>
    <p class="field-hint">记录进程退出、异常位置和数据库错误分类，供排查连接与运行故障；不包含原始异常正文、SQL 或凭据。这里不是 Agent 的思考记录。</p>
    <div class="actions"><button class="btn small" :disabled="loading" @click="load">刷新进程诊断</button><a v-if="page?.status === 'available'" class="btn small" :href="`/api${path}/export`" :download="`${run.run_id}-worker.jsonl`">导出进程诊断</a></div>
    <p v-if="loading && !page" role="status">正在读取进程诊断…</p>
    <p v-if="error" role="alert" class="note amber">{{ error }}</p>
    <p v-if="page?.incomplete" role="alert" class="note amber">诊断记录不完整，已有记录仍可查看；不能据此还原全部故障。</p>
    <p v-if="page?.status === 'not_recorded'" class="field-hint">此运行没有保存进程诊断，旧运行的底层异常无法补录。</p>
    <p v-if="page?.status === 'cleared'" class="field-hint">此运行的进程诊断已主动清理。</p>
    <div v-if="page?.events.length" class="table-scroll"><table><thead><tr><th>时间</th><th>事件</th><th>诊断</th></tr></thead><tbody>
      <tr v-for="(entry, index) in page.events" :key="index"><td>{{ time(entry.time) }}</td><td>{{ labels[entry.code] || entry.code }}</td><td>
        <div v-if="entry.stage">阶段：{{ entry.stage }}</div>
        <div v-if="entry.error_code">错误码：{{ entry.error_code }}</div>
        <div v-if="entry.exception_type">异常类型：{{ entry.exception_type }}</div>
        <template v-if="entry.database"><div>{{ categories[entry.database.category || 'unknown'] || entry.database.category }} · {{ entry.database.stage }}</div>
          <div>SQLSTATE：{{ entry.database.sqlstate || 'unknown' }}<span v-if="entry.database.errno != null"> · 驱动编号：{{ entry.database.errno }}</span></div>
          <div v-if="entry.database.cause_code">原始错误：{{ entry.database.cause_code }}</div>
          <div v-if="entry.database.recovery_decision">恢复判定：{{ recoveryDecisionLabels[entry.database.recovery_decision] || entry.database.recovery_decision }}</div>
          <div v-if="entry.database.recovery">恢复结果：{{ entry.database.recovery }}</div>
          <div v-if="entry.database.connection_closed != null">连接已关闭：{{ entry.database.connection_closed ? '是' : '否' }}</div>
          <div v-if="entry.database.connection_broken != null">驱动标记连接损坏：{{ entry.database.connection_broken ? '是' : '否' }}</div>
          <div v-if="entry.database.probe_code">检查项目：{{ probeLabels[entry.database.probe_code] || entry.database.probe_code }}</div>
          <div v-if="entry.database.probe_failure">检查结果：{{ probeFailureLabels[entry.database.probe_failure] || entry.database.probe_failure }}</div>
          <div v-if="entry.database.boundary_recovery_attempts != null">本次快照恢复次数：{{ entry.database.boundary_recovery_attempts }}<span v-if="entry.database.recovery_attempt_limit != null"> / {{ entry.database.recovery_attempt_limit }}</span></div>
          <div v-if="entry.database.terminal">该来源已停止事实查询，请检查原因后新开运行。</div>
          <p v-if="entry.database.category === 'qualification'" class="field-hint">检查未通过不等于网络断连。连接预检失败时可在额度内丢弃会话并重建；新会话必须通过全部只读和授权检查才能取数。</p>
        </template>
        <div v-if="entry.exit_code != null">退出码：{{ entry.exit_code }}</div>
        <div v-if="entry.connection_generation != null">连接版本：{{ entry.connection_generation }}</div>
        <div v-if="entry.reconnect_attempts != null">重连尝试：{{ entry.reconnect_attempts }}</div>
        <div v-if="entry.stderr_bytes != null">标准错误流：{{ entry.stderr_bytes }} 字节（原文未保存）</div>
        <div v-for="frame in entry.frames || []" :key="`${frame.file}:${frame.line}`" class="mono">{{ frame.file }}:{{ frame.line }}</div>
      </td></tr>
    </tbody></table></div>
  </details>
</template>

<style scoped>
.worker-diagnostics { margin-top: 16px; border-top: 1px solid var(--line, #e5eaf1); padding-top: 12px; }
summary { cursor: pointer; font-weight: 600; }
.actions { margin: 12px 0; }
td { vertical-align: top; overflow-wrap: anywhere; }
</style>
