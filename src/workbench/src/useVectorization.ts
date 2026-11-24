import { computed, onUnmounted, ref, watch } from 'vue'
import { ApiError, ensureSession, projectPath, request } from './api'

interface Job {
  status: 'idle' | 'running' | 'completed' | 'failed' | 'interrupted'
  job_id?: string
  error_code?: string
  progress?: { stage: 'preparing' | 'embedding' | 'publishing'; completed: number; total: number | null }
  result?: { vector_dim?: number; embedder_model?: string; vector_cards?: number }
}

/** Short requests keep long local model builds alive across page refreshes. */
export function useVectorization(projectId: () => string | undefined, completed: () => void) {
  const job = ref<Job>({ status: 'idle' })
  const error = ref('')
  const starting = ref(false)
  let timer: ReturnType<typeof setTimeout> | undefined
  let generation = 0
  let alive = true
  const running = computed(() => starting.value || job.value.status === 'running')

  function apply(value: Job, notify: boolean) {
    job.value = value
    error.value = ''
    if (value.status === 'failed' || value.status === 'interrupted') {
      error.value = new ApiError(value.error_code || 'retrieval_build_failed').message
    }
    if (value.status === 'completed' && notify) completed()
  }
  async function poll(pid: string, version: number, notify = false, failures = 0, previousJob?: string) {
    try {
      const value = await request<Job>(`${projectPath(pid)}/retrieval/jobs`)
      if (!alive || version !== generation) return
      // A timed-out POST may have started a new job. Only acknowledge a new job ID.
      if (previousJob !== undefined && (!value.job_id || value.job_id === previousJob)) return
      apply(value, notify)
      if (value.status === 'running') timer = setTimeout(() => void poll(pid, version, true), 1500)
    } catch (err) {
      if (alive && version === generation) {
        error.value = err instanceof Error ? err.message : '索引状态读取失败，请刷新后查看。'
        if (job.value.status === 'running' || failures < 3) {
          const delay = Math.min(12000, 1500 * 2 ** Math.min(failures, 3))
          timer = setTimeout(() => void poll(pid, version, notify, failures + 1, previousJob), delay)
        }
      }
    }
  }
  watch(projectId, pid => {
    clearTimeout(timer)
    const version = ++generation
    job.value = { status: 'idle' }; error.value = ''; starting.value = false
    if (pid) void poll(pid, version)
  }, { immediate: true })

  async function start() {
    const pid = projectId()
    if (!pid || running.value) return
    const version = ++generation
    clearTimeout(timer)
    starting.value = true; error.value = ''
    try {
      await ensureSession(pid)
      if (!alive || version !== generation) return
      const value = await request<Job>(`${projectPath(pid)}/retrieval/jobs`, { method: 'POST', body: '{}' })
      if (!alive || version !== generation) return
      apply(value, true)
      if (value.status === 'running') timer = setTimeout(() => void poll(pid, version, true), 500)
    } catch (err) {
      if (alive && version === generation) {
        error.value = err instanceof Error ? err.message : '索引任务启动失败。'
        if (err instanceof ApiError && ['network_unavailable', 'request_timeout', 'invalid_response'].includes(err.code)) {
          const previousJob = job.value.job_id || ''
          timer = setTimeout(() => void poll(pid, version, true, 0, previousJob), 1000)
        }
      }
    } finally {
      if (alive && version === generation) starting.value = false
    }
  }
  onUnmounted(() => { alive = false; ++generation; clearTimeout(timer) })
  return { running, error, start, job }
}
