import { afterEach, describe, expect, it, vi } from 'vitest'
import { clearSession, clearSessions, ensureSession, request } from './api'

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); clearSessions() })

describe('request feedback', () => {
  it('keeps one long retrieval request open until the caller stops waiting', async () => {
    vi.useFakeTimers()
    const caller = new AbortController()
    const fetcher = vi.fn((_url, init) => new Promise((_resolve, reject) => {
      init.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
    }))
    vi.stubGlobal('fetch', fetcher)
    const result = expect(request('/projects/test/retrieval/query', { method: 'POST', signal: caller.signal, timeoutMs: null }))
      .rejects.toMatchObject({ code: 'request_cancelled' })
    await vi.advanceTimersByTimeAsync(180000)
    expect(fetcher.mock.calls[0]![1].signal.aborted).toBe(false)
    caller.abort()
    await result
    expect(fetcher).toHaveBeenCalledTimes(1)
  })
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

const sessionReply = (token: string) => Response.json({ session_id: token, expires_in_seconds: 3600 })
const staleSession = (rejected = true) => Response.json({ error: { code: 'credential_session_invalid' } }, {
  status: 400, headers: rejected ? { 'X-Workbench-Session-Rejected': '1' } : {},
})

describe('project session renewal', () => {
  it('renews an expired token once on the next use and keeps the write revision', async () => {
    vi.useFakeTimers()
    const fetcher = vi.fn()
      .mockResolvedValueOnce(sessionReply('one'))
      .mockResolvedValueOnce(sessionReply('two'))
      .mockResolvedValueOnce(Response.json({}))
    vi.stubGlobal('fetch', fetcher)
    await ensureSession('test')
    await vi.advanceTimersByTimeAsync(3600000)
    await Promise.all([ensureSession('test'), ensureSession('test')])
    await request('/projects/test/runs', { method: 'POST', headers: { 'If-Match': 'rev' }, body: '{}' })
    expect(fetcher.mock.calls.map(([url]) => url)).toEqual(['/api/projects/test/session', '/api/projects/test/session', '/api/projects/test/runs'])
    expect(fetcher.mock.calls[2]![1].headers).toMatchObject({ 'X-Workbench-Session': 'two', 'If-Match': 'rev' })
  })

  it('recovers concurrent requests after a server restart and honours cancellation during renewal', async () => {
    let creations = 0
    let finish!: (value: Response) => void
    const writes: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit) => {
      if (url.endsWith('/session')) {
        if (++creations === 1) return sessionReply('old')
        return new Promise<Response>(resolve => { finish = resolve })
      }
      if (new Headers(init.headers).get('X-Workbench-Session') === 'old') return staleSession()
      writes.push(String(init.body))
      return Response.json({ done: true })
    }))
    await ensureSession('test')
    const caller = new AbortController()
    const url = '/projects/test/runs'
    const first = request(url, { method: 'POST', body: 'first' })
    const cancelled = expect(request(url, { method: 'POST', body: 'second', signal: caller.signal }))
      .rejects.toMatchObject({ code: 'request_cancelled' })
    await vi.waitFor(() => expect(finish).toBeDefined())
    caller.abort()
    await cancelled
    finish(sessionReply('new'))
    await expect(first).resolves.toEqual({ done: true })
    expect(writes).toEqual(['first'])
    expect(creations).toBe(2)
  })

  it('only resends a write on pre-dispatch rejection, at most once', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(sessionReply('old'))
      .mockResolvedValueOnce(staleSession(false))
      .mockResolvedValueOnce(staleSession())
      .mockResolvedValueOnce(sessionReply('new'))
      .mockResolvedValueOnce(staleSession())
    vi.stubGlobal('fetch', fetcher)
    await ensureSession('test')
    const write = () => request('/projects/test/runs', { method: 'POST', body: '{}' })
    await expect(write()).rejects.toMatchObject({ code: 'credential_session_invalid' })
    expect(fetcher).toHaveBeenCalledTimes(2)
    await expect(write()).rejects.toMatchObject({ code: 'credential_session_invalid' })
    expect(fetcher).toHaveBeenCalledTimes(5)
  })

  it('does not reinstall a pending session after clearing a project', async () => {
    let finish!: (value: Response) => void
    const fetcher = vi.fn().mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
      .mockResolvedValueOnce(sessionReply('new')).mockResolvedValueOnce(Response.json({}))
    vi.stubGlobal('fetch', fetcher)
    const discarded = expect(ensureSession('test')).rejects.toMatchObject({ code: 'request_cancelled' })
    clearSession('test')
    await ensureSession('test')
    finish(sessionReply('old'))
    await discarded
    await request('/projects/test/credentials')
    expect(fetcher.mock.calls[2]![1].headers).toMatchObject({ 'X-Workbench-Session': 'new' })
  })
})
