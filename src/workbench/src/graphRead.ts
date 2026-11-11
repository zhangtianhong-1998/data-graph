import { ApiError, request } from './api'

/** Initial graph reads and continuation pages share bounded recovery. Writes,
 * revoked access and changed publications never enter this retry path. */
export async function readGraph<T>(path: string, signal: AbortSignal): Promise<T> {
  for (let attempt = 0; ; attempt++) {
    if (signal.aborted) throw new ApiError('request_cancelled')
    try {
      const value = await request<T>(path, { signal })
      if (signal.aborted) throw new ApiError('request_cancelled')
      return value
    } catch (error) {
      if (signal.aborted || attempt >= 2 || !(error instanceof ApiError)
        || (!['network_unavailable', 'request_timeout', 'http_502', 'http_503', 'http_504'].includes(error.code)
          && ![502, 503, 504].includes(error.status))) throw error
      await new Promise<void>(resolve => {
        const done = () => { clearTimeout(timer); signal.removeEventListener('abort', done); resolve() }
        const timer = setTimeout(done, 100 * (attempt + 1))
        signal.addEventListener('abort', done, { once: true })
      })
    }
  }
}
