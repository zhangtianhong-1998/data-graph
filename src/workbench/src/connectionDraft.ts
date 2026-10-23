import { ensureSession, json, request } from './api'
import type { DatabaseForm } from './database'
import type { ConnectionForm } from './onboarding'

export interface ConnectionDraft {
  form: DatabaseForm
  connection: ConnectionForm
  mode: 'direct' | 'environment'
  retention: boolean
  checked: string[]
}
export interface DraftResult { saved: boolean; draft?: ConnectionDraft; revision: number }
export type DraftStatus = 'idle' | 'saving' | 'saved' | 'failed' | 'off'

/** Keep credentials in memory until the backend encrypts them; never use browser storage. */
export class ConnectionDraftWriter {
  private revision = 0
  private acknowledged = 0
  private latest: { draft: ConnectionDraft; revision: number } | undefined
  private timer: ReturnType<typeof setTimeout> | undefined
  private queue: Promise<unknown> = Promise.resolve()
  private enabled = true
  private conflicted = false

  constructor(private projectId: string, private path: string,
    private report: (status: DraftStatus, error?: string) => void) {}

  restore(revision: number) {
    if (Number.isSafeInteger(revision) && revision >= 0) this.revision = Math.max(this.revision, revision)
    this.acknowledged = this.revision
  }
  activate() { this.enabled = true }
  private nextRevision() { this.revision = Math.max(Date.now(), this.revision + 1); return this.revision }
  private cancelTimer() { if (this.timer) clearTimeout(this.timer); this.timer = undefined }

  update(draft: ConnectionDraft) {
    if (!this.enabled) return
    this.conflicted = false
    // Freeze the form now so later edits/secret cleanup cannot mutate an in-flight write.
    this.latest = { draft: structuredClone(draft), revision: this.nextRevision() }
    this.report('saving')
    this.cancelTimer()
    this.timer = setTimeout(() => { void this.flush().catch(() => {}) }, 250)
  }
  flush(keepalive = false): Promise<void> {
    this.cancelTimer()
    const payload = this.latest
    if (this.conflicted && this.enabled) return Promise.reject(new Error('其他页面更新了连接草稿，请确认当前填写内容后重试保存。'))
    if (!this.enabled || !payload || payload.revision <= this.acknowledged) return Promise.resolve()
    const write = async () => {
      try {
        if (!keepalive) await ensureSession(this.projectId)
        if (!this.enabled) return
        // Unload must dispatch immediately, not wait for an older request. Server revisions
        // prevent that older request from overwriting this form if it arrives last.
        const result = await request<DraftResult>(this.path, {
          method: 'PUT', body: json(payload), keepalive,
        })
        if (result.revision > payload.revision) {
          this.revision = Math.max(this.revision, result.revision)
          if (this.enabled && payload.revision === this.latest?.revision) this.conflicted = true
          throw new Error('其他页面更新了连接草稿，请确认当前填写内容后重试保存。')
        } else {
          this.acknowledged = Math.max(this.acknowledged, payload.revision)
          if (this.enabled && payload.revision === this.latest?.revision) this.report('saved')
        }
      } catch (error) {
        if (this.enabled && payload.revision === this.latest?.revision) this.report('failed', error instanceof Error ? error.message : '请重试。')
        throw error
      }
    }
    if (keepalive) return write()
    const operation = this.queue.then(write)
    this.queue = operation.catch(() => {})
    return operation
  }
  async erase() {
    this.enabled = false
    this.cancelTimer()
    this.latest = undefined
    const revision = this.nextRevision()
    const operation = this.queue.then(async () => {
      await ensureSession(this.projectId)
      const result = await request<DraftResult>(this.path, { method: 'DELETE', body: json({ revision }) })
      this.revision = Math.max(this.revision, result.revision || 0)
      if (result.saved || result.revision > revision) throw new Error('其他页面更新了连接草稿，请确认后重试删除。')
      this.acknowledged = revision
      if (!this.enabled) this.report('off')
    })
    this.queue = operation.catch(() => {})
    try { await operation }
    catch (error) {
      this.report('failed', error instanceof Error ? error.message : '请重试。')
      throw error
    }
  }
}
