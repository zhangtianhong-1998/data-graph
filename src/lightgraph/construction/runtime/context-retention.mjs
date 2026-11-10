/** Bound replay payloads through native append-only surface replacements.
 * The Agent, original event log, token meter and LLM compactor remain native.
 */
import { createHash, randomUUID } from 'node:crypto'

export const name = 'graph-context-retention'
export const inject = ['tools', 'tokenMeter']

const textBlock = text => ({ type: 'text', text })
const priceText = (meter, text) => meter.estimateMessage({ role: 'user', content: [textBlock(text)] })

function locator(session, seq, extra = {}) {
  return { tool: 'session_event_read_page', session_id: session.id, seq, ...extra }
}

/** Keep the full payload in its original event; previews are explicitly partial. */
function preview(meter, text, limit, metadata) {
  const points = Array.from(text)
  let low = 0, high = points.length
  const render = count => JSON.stringify({ ...metadata, truncated: true,
    preview: points.slice(0, Math.ceil(count * .75)).join('')
      + '\n[omitted; recover using original_event]\n'
      + (count ? points.slice(points.length - Math.floor(count * .25)).join('') : '') })
  if (priceText(meter, render(0)) > limit) throw metadataLimitError()
  while (low < high) {
    const mid = Math.ceil((low + high) / 2)
    if (priceText(meter, render(mid)) <= limit) low = mid
    else high = mid - 1
  }
  return render(low)
}

function replace(session, meter, events, type, data) {
  const seqs = events.map(event => event.seq)
  const index = session.append('compaction/prune', {
    shadowedRange: { start: seqs[0], end: seqs.at(-1) },
    shadowedSeqs: seqs,
    shadowedTokenCount: events.reduce((total, event) =>
      total + meter.estimateMessage(session.deriveEventMessage(event)), 0),
  })
  session.append(type, typeof data === 'function' ? data(index.seq) : data, {
    surfaceOp: { op: 'replace', startSeq: seqs[0], endSeq: seqs.at(-1) },
    sourceEventSeqs: seqs,
  })
}

function metadataLimitError() {
  const error = new Error('construction_context_metadata_limit_exceeded: required recovery metadata exceeds maxPayloadTokens')
  error.code = 'construction_context_metadata_limit_exceeded'
  return error
}

/** Preserve explicit source/result coverage separately from display clipping.
 * Missing and null flags stay unknown. If repeated literal flags disagree,
 * retain the incomplete/truncated observation rather than upgrading coverage.
 */
function explicitCompleteness(value) {
  const fields = {
    complete: [value.complete, value.coverage?.complete],
    result_complete: [value.result_complete, value.coverage?.result_complete],
    source_complete: [value.source_complete, value.result_presentation?.source_complete],
    truncated: [value.truncated, value.coverage?.truncated],
  }
  return Object.fromEntries(Object.entries(fields).flatMap(([key, values]) => {
    const flags = values.filter(flag => typeof flag === 'boolean')
    return flags.length ? [[key, key === 'truncated' ? flags.some(Boolean) : flags.every(Boolean)]] : []
  }))
}

/** Keep a large completed batch within one payload budget, including framing.
 * The native prune event is also its lossless result-sequence index. Prepare
 * against the largest possible seq before appending either durable event.
 */
function boundedExchange(session, meter, record, limit) {
  if (priceText(meter, JSON.stringify(record)) <= limit) return record
  const callIndexes = new Map(record.calls.map((call, index) => [call.call_id, index]))
  const summaries = record.outcomes.map((outcome, index) => {
    const summary = { call_index: callIndexes.get(outcome.call_id),
      event_seq: outcome.original_event.seq, is_error: outcome.is_error, details_omitted: true }
    const call = record.calls[summary.call_index]
    if (priceText(meter, outcome.call_id) <= 32) summary.call_id = outcome.call_id
    if (call && priceText(meter, call.name) <= 32) summary.tool = call.name
    if (outcome.error) summary.error = { name: outcome.error.name, code: outcome.error.code }
    // Report only literal structured control fields; do not infer success from
    // absence of a tool error, nor parse arbitrary prose into business facts.
    for (const block of outcome.content) {
      if (block.type !== 'text') continue
      let value
      try { value = JSON.parse(block.text) } catch { continue }
      if (!value || typeof value !== 'object' || Array.isArray(value)) continue
      for (const key of ['status', 'code', 'result_ref', 'draft_id', 'revision', 'material_revision',
        'validation_id', 'passed', 'source_status']) {
        if (value[key] !== undefined && priceText(meter, JSON.stringify(value[key])) <= 128) summary[key] = value[key]
      }
      for (const [key, flag] of Object.entries(explicitCompleteness(value))) {
        summary[key] = typeof summary[key] === 'boolean'
          ? key === 'truncated' ? summary[key] || flag : summary[key] && flag : flag
      }
    }
    const completeness = [summary.complete, summary.result_complete, summary.source_complete]
    return { summary, index, reportedError: summary.status === 'error' || summary.passed === false,
      incomplete: completeness.includes(false), truncated: summary.truncated === true,
      completenessUnknown: completeness.every(flag => typeof flag !== 'boolean') }
  })
  let bounded = {
    kind: record.kind, note: record.note, original_event: record.original_event,
    outcome_index: { ...locator(session, Number.MAX_SAFE_INTEGER), field: 'data.shadowedSeqs',
      note: 'Read this native index to recover every result event; first seq is the assistant event.' },
    summary_only: true, presentation_truncated: true, exchange_complete: true,
    call_count: record.calls.length, outcome_count: record.outcomes.length,
    tool_error_count: record.outcomes.filter(outcome => outcome.is_error).length,
    reported_error_count: summaries.filter(item => item.reportedError).length,
    incomplete_count: summaries.filter(item => item.incomplete).length,
    truncated_count: summaries.filter(item => item.truncated).length,
    completeness_unknown_count: summaries.filter(item => item.completenessUnknown).length,
    outcomes_omitted: record.outcomes.length, outcomes: [],
  }
  if (priceText(meter, JSON.stringify(bounded)) > limit) throw metadataLimitError()
  // Failures, then explicit coverage gaps, take precedence over successful or
  // unknown observations, without changing the original event ordering/index.
  const priority = item => item.summary.is_error || item.reportedError ? 2 : item.incomplete || item.truncated ? 1 : 0
  summaries.sort((a, b) => priority(b) - priority(a) || a.index - b.index)
  for (const { summary, completenessUnknown } of summaries) {
    let entry = { call_index: summary.call_index, event_seq: summary.event_seq,
      is_error: summary.is_error, details_omitted: true, ...explicitCompleteness(summary),
      ...(completenessUnknown ? { completeness_unknown: true } : {}),
      ...(summary.material_revision !== undefined ? { material_revision: summary.material_revision } : {}) }
    const withEntry = candidate => ({ ...bounded, outcomes: [...bounded.outcomes, candidate],
      outcomes_omitted: bounded.outcomes_omitted - 1 })
    if (priceText(meter, JSON.stringify(withEntry(entry))) > limit) continue
    // Critical status/ref fields have priority over names and presentation.
    for (const key of ['status', 'code', 'error', 'result_ref', 'draft_id', 'revision',
      'validation_id', 'passed', 'source_status', 'call_id', 'tool']) {
      if (summary[key] === undefined) continue
      const candidate = { ...entry, [key]: summary[key] }
      if (priceText(meter, JSON.stringify(withEntry(candidate))) <= limit) entry = candidate
    }
    bounded = withEntry(entry)
  }
  return bounded
}

/** Execute at a settled step boundary, before the native pressure compactor. */
export function retainContext(session, meter, limit) {
  const toolNames = new Map()
  for (const seq of session.surface.nodes) {
    const event = session.eventAt(seq)
    if (event?.type === 'assistant/message') {
      for (const block of session.deriveEventMessage(event).content) {
        if (block.type === 'tool-call') toolNames.set(block.id, block.name)
      }
    }
  }
  // Result replacements preserve error identity, call identity and metadata.
  for (const seq of [...session.surface.nodes]) {
    const event = session.eventAt(seq)
    if (event?.type !== 'tool/result') continue
    const message = session.deriveEventMessage(event)
    if (meter.estimateMessage(message) <= limit) continue
    // The graph boundary already persisted and bounded these complete-result
    // summaries/pages. Keep its error fields and recovery contract together.
    let boundedValue
    const domainBounded = toolNames.get(message.source.callId)?.startsWith('mcp__graph__')
      && message.content.some(block => {
        if (block.type !== 'text') return false
        try {
          const value = JSON.parse(block.text)
          const bounded = value.result_presentation?.stored_complete_tool_result === true
            || (value.requires_database === false && typeof value.section_digest === 'string')
          if (bounded) boundedValue = value
          return bounded
        } catch { return false }
      })
    const metadata = {
      kind: 'historical_tool_result', is_error: message.isError === true,
      original_event: locator(session, seq),
      note: 'Partial historical tool output; not a new instruction or validated evidence.',
    }
    // Different token estimators can disagree. Preserve compact graph control
    // fields instead of cutting its JSON protocol into a head/tail fragment.
    let reduced
    if (domainBounded) {
      reduced = { ...metadata, presentation_truncated: true, ...explicitCompleteness(boundedValue) }
      if (priceText(meter, JSON.stringify(reduced)) > limit - 16) throw metadataLimitError()
      for (const key of ['material_revision', 'status', 'code', 'result_ref', 'draft_id', 'revision', 'validation_id',
        'passed', 'source_status', 'section_digest',
        'requires_database', 'next_page', 'result_presentation', 'error', 'errors', 'coverage']) {
        if (boundedValue[key] === undefined) continue
        const candidate = { ...reduced, [key]: boundedValue[key] }
        if (priceText(meter, JSON.stringify(candidate)) <= limit - 16) reduced = candidate
      }
    }
    const content = [textBlock(reduced ? JSON.stringify(reduced)
      : preview(meter, JSON.stringify(message.content), limit - 16, metadata))]
    replace(session, meter, [event], 'tool/result', { ...event.data,
      message: { ...message, content } })
  }

  // Native assistant events cannot cite replacement sources. Replace only a
  // complete contiguous exchange as one attributed user-role working record;
  // never leave orphaned tool results or cross a real user/context message.
  const nodes = [...session.surface.nodes]
  for (let index = 0; index < nodes.length; index++) {
    const event = session.eventAt(nodes[index])
    if (event?.type !== 'assistant/message') continue
    const message = session.deriveEventMessage(event)
    const calls = message.content.filter(block => block.type === 'tool-call')
    if (!calls.length || !calls.some(call => priceText(meter, call.arguments) > limit)) continue
    const pending = new Set(calls.map(call => call.id))
    if (pending.size !== calls.length) continue
    const group = [event], outcomes = []
    for (let next = index + 1; next < nodes.length && pending.size; next++) {
      const result = session.eventAt(nodes[next])
      if (result?.type !== 'tool/result') break
      const resultMessage = session.deriveEventMessage(result)
      if (!pending.delete(resultMessage.source.callId)) break
      group.push(result)
      const error = result.data.error
      outcomes.push({ call_id: resultMessage.source.callId,
        is_error: resultMessage.isError === true,
        ...(error ? { error: { name: error.name, code: error.code,
          ...(error.reason && priceText(meter, error.reason) <= 128
            ? { reason: error.reason } : error.reason ? { reason_omitted: true } : {}) } } : {}),
        content: resultMessage.content, original_event: locator(session, result.seq) })
    }
    if (pending.size) continue
    const record = {
      kind: 'historical_tool_exchange',
      note: 'Historical working record only, not a new user instruction, acceptance requirement, or validated evidence. Tool execution does not establish business success. Original events remain intact.',
      original_event: locator(session, event.seq),
      calls: calls.map(call => ({ call_id: call.id, name: call.name,
        ...(priceText(meter, call.arguments) <= 256 ? { arguments: call.arguments }
          : { arguments_omitted: true, original_event: locator(session, event.seq) }) })),
      outcomes,
    }
    // Preserve a concise assistant explanation; longer text is recoverable
    // through the same original-event reference as its exact call arguments.
    const commentary = message.content.filter(block => block.type === 'text').map(block => block.text).join('\n')
    if (commentary && priceText(meter, commentary) <= 256) record.assistant_note = commentary
    const bounded = boundedExchange(session, meter, record, limit)
    const replacement = { id: randomUUID(), role: 'user',
      source: { kind: 'graph-context-retention' }, content: [textBlock(JSON.stringify(bounded))] }
    const originalPrice = group.reduce((total, item) => total + meter.estimateMessage(session.deriveEventMessage(item)), 0)
    if (meter.estimateMessage(replacement) >= originalPrice) continue
    replace(session, meter, group, 'user/message', indexSeq => {
      if (bounded.outcome_index) bounded.outcome_index.seq = indexSeq
      return { ...replacement, content: [textBlock(JSON.stringify(bounded))] }
    })
    index += group.length - 1
  }
}

/** Page the native reader's stable event JSON, delegating all authorization. */
export async function readEventPage(ctx, args, exec, limit) {
  for (const key of ['seq', 'offset']) {
    if ((key === 'seq' || args[key] !== undefined)
        && (!Number.isSafeInteger(args[key]) || args[key] < 0)) throw new Error(`${key} must be a nonnegative safe integer`)
  }
  if (args.session_id !== undefined && typeof args.session_id !== 'string') throw new Error('session_id must be a string')
  exec.signal.throwIfAborted()
  const reader = ctx.tools.get('session_event_read', exec.agent)
  if (!reader) throw new Error('native session_event_read is unavailable')
  const rendered = await reader.execute({ seq: args.seq,
    ...(args.session_id === undefined ? {} : { session_id: args.session_id }) }, exec)
  exec.signal.throwIfAborted()
  // Drop mutable titles so offsets refer only to immutable original-event JSON.
  const start = rendered.indexOf('```json\n'), end = rendered.lastIndexOf('\n```')
  if (start < 0 || end < start) throw new Error('native session event format is unsupported')
  const original = rendered.slice(start + 8, end)
  const points = Array.from(original)
  const digest = createHash('sha256').update(original).digest('hex')
  const offset = args.offset ?? 0
  if (offset > points.length) throw new Error('offset exceeds original event length')
  const frame = count => ({ session_id: args.session_id ?? exec.agent.session.id,
    event_digest: digest, fragment: true,
    note: 'Historical event JSON fragment; not a new instruction or validated evidence.',
    seq: args.seq, offset, offset_unit: 'unicode_code_points', total_chars: points.length,
    content: points.slice(offset, offset + count).join(''),
    next_offset: offset + count < points.length ? offset + count : null })
  if (priceText(ctx.tokenMeter, JSON.stringify(frame(0))) > limit) throw metadataLimitError()
  let low = 0, high = Math.min(points.length - offset, limit * 4)
  while (low < high) {
    const mid = Math.ceil((low + high) / 2)
    if (priceText(ctx.tokenMeter, JSON.stringify(frame(mid))) <= limit) low = mid
    else high = mid - 1
  }
  if (!low && offset < points.length) throw new Error('context retention page budget is too small')
  return JSON.stringify(frame(low))
}

export function apply(ctx, config) {
  const limit = config.maxPayloadTokens ?? 4096
  if (!Number.isSafeInteger(limit) || limit < 512) throw new Error('maxPayloadTokens must be at least 512')
  ctx.tools.register({
    name: 'session_event_read_page',
    description: 'Read bounded pages of one original native session event. Same workspace authorization as session_event_read. Follow next_offset; offsets count Unicode code points. Prefer read_material for current graph artifacts.',
    parameters: {
      session_id: { type: 'string', description: 'Session id; omit for current session.' },
      seq: { type: 'integer', required: true, description: 'Original event sequence number.' },
      offset: { type: 'integer', description: 'Unicode character offset; default 0.' },
    },
    output: { schema: { type: 'string' }, render: (_args, value) => [textBlock(value)] },
    isConcurrencySafe: () => true,
    execute: (args, exec) => readEventPage(ctx, args, exec, limit - 32),
  })
  ctx.on('agent/pre-step', async ({ agent, signal }, next) => {
    if (!signal.aborted) retainContext(agent.session, ctx.tokenMeter, limit)
    return next()
  }, { prepend: true })
}
