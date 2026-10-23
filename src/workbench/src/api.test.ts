import { afterEach, describe, expect, it, vi } from 'vitest'
import { clearSessions, ensureSession, request } from './api'

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); clearSessions() })

describe('request feedback', () => {
  it('stops waiting on a stalled request and reports a timeout', async () => {
    vi.useFakeTimers()
    let signal: AbortSignal | undefined
    vi.stubGlobal('fetch', vi.fn((_url, init) => new Promise((_resolve, reject) => {
      signal = init.signal
      signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
    })))
    const result = expect(request('/projects/test/database/test', { timeoutMs: 50 })).rejects.toMatchObject({ code: 'request_timeout' })
    await vi.advanceTimersByTimeAsync(50)
    await result
    expect(signal?.aborted).toBe(true)
  })

  it('also bounds a response whose body never finishes', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('fetch', vi.fn(async (_url, init) => ({ ok: true, status: 200, json: () => new Promise((_resolve, reject) => {
      init.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
    }) })))
    const result = expect(request('/projects/test/database/tables', { timeoutMs: 50 })).rejects.toMatchObject({ code: 'request_timeout' })
    await vi.advanceTimersByTimeAsync(50)
    await result
  })

  it('does not expose HTML or raw connection errors', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('<html>private password</html>', { status: 502 })))
    await expect(request('/projects/test/session')).rejects.toMatchObject({ code: 'invalid_response' })
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('private password') }))
    await expect(request('/projects/test/session')).rejects.toMatchObject({ code: 'network_unavailable' })
  })

  it('allows session establishment to be retried after a network failure', async () => {
    const fetcher = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(new Response(JSON.stringify({ session_id: 'test-token' })))
    vi.stubGlobal('fetch', fetcher)
    await expect(ensureSession('test')).rejects.toMatchObject({ code: 'network_unavailable' })
    await ensureSession('test')
    expect(fetcher).toHaveBeenCalledTimes(2)
  })
})
