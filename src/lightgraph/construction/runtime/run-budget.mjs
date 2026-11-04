/** Bound every Harness model dispatch, including retry and compaction calls.
 * No transport parsing or business inference is performed by this plugin.
 */
import { writeFileSync, renameSync, appendFileSync } from 'node:fs'
import { randomUUID } from 'node:crypto'

export const name = 'graph-run-budget'
export const inject = ['llm', 'tools', 'agents', 'tokenMeter']

export function apply(ctx, config) {
  if (!Number.isSafeInteger(config.maxCalls) || config.maxCalls < 1
      || !Number.isFinite(config.maxRunSeconds) || config.maxRunSeconds <= 0) {
    throw new Error('Invalid graph run budget')
  }
  let calls = 0
  let stopped = ''
  let diagnostic = null
  let peakInputTokens = 0
  const executionId = randomUUID()
  const permitted = name => name.startsWith('mcp__graph__') || [
    'skill', 'session_search', 'session_event_search', 'session_trace',
    'session_event_trace', 'session_event_read',
  ].includes(name)
  // Denial survives accidental profile additions and subsequent plugins.
  ctx.tools.guard(exec => permitted(exec.name) ? undefined : 'construction_tool_not_allowed')
  const restricted = new WeakSet()
  ctx.on('agent/pre-step', async ({ agent }, next) => {
    if (!restricted.has(agent)) {
      agent.ctx.tools.restrict({ allow: ctx.tools.schemas().map(tool => tool.name).filter(permitted) })
      restricted.add(agent)
    }
    return next()
  })
  const start = performance.now()
  function save(stopReason = '') {
    if (stopReason) stopped = stopReason
    const text = `scope: harness_llm_stream_dispatch\ncalls: ${calls}\nmax_calls: ${config.maxCalls}\n`
      + `execution_id: '${executionId}'\n`
      + `elapsed_seconds: ${(performance.now() - start) / 1000}\nstop_reason: '${stopped}'\n`
      + `estimated_input_peak: ${peakInputTokens}\ninput_diagnostic: ${JSON.stringify(diagnostic)}\n`
    writeFileSync(`${config.output}.tmp`, text, { encoding: 'utf8', mode: 0o600 })
    renameSync(`${config.output}.tmp`, config.output)
  }
  save()
  ctx.on('llm/stream', async function* (options, next) {
    const elapsed = (performance.now() - start) / 1000
    const code = elapsed >= config.maxRunSeconds ? 'construction_time_budget_exhausted'
      : calls >= config.maxCalls ? 'construction_call_budget_exhausted' : null
    if (code) {
      save(code)
      const error = new Error(code)
      error.code = code
      throw error
    }
    // Price the actual request with the upstream meter. Auxiliary summary
    // requests contain a selected history plus their own instructions; pricing
    // the whole parent session would prevent the compression needed to recover.
    let estimate = (options.messages ?? []).reduce((total, message) =>
      total + ctx.tokenMeter.estimateMessage(message), 0)
    if (options.system) estimate += ctx.tokenMeter.estimateMessage({
      role: 'system', content: [{ type: 'text', text: options.system }] })
    if (options.tools?.length) estimate += ctx.tokenMeter.estimateMessage({
      role: 'user', content: [{ type: 'text', text: JSON.stringify(options.tools) }] })
    let basis = 'native_request_estimate'
    const agent = options.sessionId && !options.purpose ? ctx.agents.get(options.sessionId) : undefined
    if (agent) {
      const measured = ctx.tokenMeter.measure(agent.session)
      estimate = Math.max(estimate, measured.totalTokens)
      basis = measured.baseline.kind === 'usage' ? 'native_usage_calibrated_estimate' : basis
    }
    peakInputTokens = Math.max(peakInputTokens, estimate)
    if (Number.isSafeInteger(config.maxInputTokens) && estimate > config.maxInputTokens) {
      diagnostic = { code: 'construction_input_limit_exceeded', estimated_input_tokens: estimate,
        max_input_tokens: config.maxInputTokens, measurement: basis,
        purpose: options.purpose ?? 'agent', exact: false }
      save(diagnostic.code)
      const error = new Error(diagnostic.code)
      error.code = diagnostic.code
      throw error
    }
    calls += 1
    const dispatchIndex = calls
    save()
    if (config.requestLog) {
      const { provider, model, messages, tools, system, maxTokens, temperature, reasoningEffort } = options
      const record = { type: 'model/request', seq: calls, time: Date.now(),
        execution_id: executionId,
        purpose: options.purpose ?? 'agent', session_id: options.sessionId,
        estimated_input_tokens: estimate, measurement: basis, exact: false,
        request: { provider, model, messages, tools, system, maxTokens, temperature, reasoningEffort } }
      appendFileSync(config.requestLog, JSON.stringify(record) + '\n', { encoding: 'utf8', mode: 0o600 })
    }
    let usage = null
    let finish = null
    try {
      for await (const chunk of next()) {
        // Usage chunks are cumulative for one request; only the last sample
        // counts. Cache buckets are disjoint in the native TokenUsage contract.
        if (chunk.type === 'usage') usage = chunk.usage
        if (chunk.type === 'finish') finish = chunk.reason?.kind ?? null
        yield chunk
      }
    } finally {
      if (config.usageLog) appendFileSync(config.usageLog, JSON.stringify({
        type: 'model/usage', seq: dispatchIndex, time: Date.now(),
        execution_id: executionId,
        session_id: options.sessionId, purpose: options.purpose ?? 'agent',
        usage, finish_reason: finish,
      }) + '\n', { encoding: 'utf8', mode: 0o600 })
      save()
    }
  })
}
