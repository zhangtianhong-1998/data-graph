"""Reuse exact task context in tool results without changing evidence content.

All references point into the fixed user packet, which history-window pruning
preserves. No result depends on another removable tool exchange. The canonical
packet, tool transcript and evidence registry are never modified.
"""
from __future__ import annotations

import json
import math

from lightgraph.contracts import canonical
from .token_estimation import estimate_tokens


def _object(content):
    if not isinstance(content, str):
        return None
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate_key')
            result[key] = value
        return result
    def nonfinite(_):
        raise ValueError('nonfinite')
    def finite_float(text):
        value=float(text)
        if not math.isfinite(value):nonfinite(None)
        return value
    try:
        value = json.loads(content, object_pairs_hook=unique, parse_constant=nonfinite,
                           parse_float=finite_float)
    except (ValueError, TypeError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def deduplicate_request_context(messages):
    """Return a request copy and value-free counts for exact context reuse.

    Document read state, scope, revisions and list order must all match. A prior
    partial document, a different table window, or any unknown field stays in
    the tool result. Cell values, tool arguments and validation feedback are
    always left untouched. Approximate token savings cover message JSON only;
    tools and output schemas are unchanged by this projection.
    """
    metrics = {'reused_fields': 0, 'projected_results': 0, 'saved_estimated_tokens': 0}
    fitted = list(messages)
    packet = None
    for message in messages:
        if message.get('role') != 'user':
            continue
        envelope = _object(message.get('content'))
        if envelope and isinstance(envelope.get('input'), dict) and 'output_schema' in envelope:
            packet = envelope['input']
            break
    if packet is None:
        return fitted, metrics
    fields = {'document_refs': 'document_refs', 'tables': 'context_tables',
              'documents': 'document_snippets'}
    for index, message in enumerate(messages):
        if message.get('role') != 'tool':
            continue
        result = _object(message.get('content'))
        if result is None or not isinstance(result.get('context'), dict):
            continue
        context = result['context']
        reused = 0
        for field, target in fields.items():
            original = context.get(field)
            if (field + '_ref' not in context and isinstance(original, list) and original
                    and isinstance(packet.get(target), list)
                    and canonical(original) == canonical(packet[target])):
                # Comparing canonical JSON preserves scalar types, including
                # the otherwise equal Python values True, 1 and 1.0.
                del context[field]
                context[field + '_ref'] = 'input.' + target
                reused += 1
        if not reused:
            continue
        projected = {**message, 'content': canonical(result)}
        before = estimate_tokens(canonical(message))
        after = estimate_tokens(canonical(projected))
        if after >= before:
            continue
        fitted[index] = projected
        metrics['reused_fields'] += reused
        metrics['projected_results'] += 1
        metrics['saved_estimated_tokens'] += before - after
    return fitted, metrics
