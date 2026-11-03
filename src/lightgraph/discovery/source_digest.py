"""Request-only digests of source results, never replacements for source evidence.

Like Hermes' proactive tool-result pruning, this keeps the executed call intact
and projects only its older result. No model, disk cache, or new reference is
involved. Raw transcripts and the registered evidence remain owned by the caller.
"""
from __future__ import annotations

import json

from lightgraph.contracts import canonical
from .token_estimation import estimate_tokens


SOURCE_TOOLS = frozenset({
    'get_metadata', 'search_knowledge', 'profile_columns', 'check_fd',
    'check_reference', 'execute_readonly_sql',
})
_COUNTS = frozenset({
    'rows', 'non_null', 'distinct_exact', 'nulls', 'empty_str', 'min_len', 'max_len',
    'distinct_tuples', 'duplicate_groups', 'max_duplicate', 'null_key_rows',
    'x_groups', 'violating_groups', 'excess_values', 'max_values_per_x',
    'all_null_groups', 'mixed_null_groups', 'null_attribute_rows',
    'source_ref_tuples', 'null_refs', 'partial_null_refs', 'unmatched',
    'ambiguous', 'unique_matches',
})
_COVERAGE = frozenset({
    'source_scope', 'result_complete', 'truncated', 'source_scan',
    'requested_limit', 'returned_rows', 'full_source_validation', 'selection',
    'semantic_relationship_confirmed', 'comparison_complete', 'source_complete',
    'target_complete', 'version_stable',
})
_HEADER_FIELDS = ('id', 'name', 'type', 'position', 'key', 'sql_identifier')
_REFERENCE_FIELDS = ('doc_id', 'revision', 'section_id', 'locator', 'reference',
                     'document_version_reference', 'read_state', 'purpose', 'evidence_role')
_LIMIT = 64


def decoded_result(content):
    """Malformed responses are not repaired or interpreted as successful probes."""
    if not isinstance(content, str):
        return None
    try:
        value = json.loads(content)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _scalar(value):
    return value is None or type(value) in (bool, int, float) or (
        isinstance(value, str) and len(value) <= 512)


def _fields(value, names):
    return {key: value[key] for key in names if key in value and _scalar(value[key])}


def _headers(value):
    if not isinstance(value, list):
        return []
    return [item if isinstance(item, str) else _fields(item, _HEADER_FIELDS)
            for item in value[:_LIMIT]
            if (isinstance(item, str) and len(item) <= 512) or isinstance(item, dict)]


def _counts(value):
    if not isinstance(value, dict):
        return {}
    return {key: number for key, number in value.items()
            if key in _COUNTS and type(number) is int and number >= 0}


def _references(value):
    if not isinstance(value, list):
        return []
    # Preserve identifiers exactly; neither construct a reference nor promote a
    # partially read document to complete when its text leaves the request.
    return [_fields(item, _REFERENCE_FIELDS) for item in value[:_LIMIT]
            if isinstance(item, dict)]


def _data_digest(data):
    if not isinstance(data, dict):
        return {}
    result = _fields(data, ('row_format', 'page', 'next_page', 'complete', 'row_count',
                           'result_complete', 'truncated', 'selection'))
    for key in ('columns', 'column_bindings'):
        if isinstance(data.get(key), list):
            result[key] = _headers(data[key])
            result[key + '_count'] = len(data[key])
    if isinstance(data.get('rows'), list):
        # This is the number of rows in the old result, not a table cardinality.
        result['omitted_result_rows'] = len(data['rows'])
    if isinstance(data.get('strata_samples'), list):
        result['omitted_sample_groups'] = len(data['strata_samples'])
    if isinstance(data.get('sections'), list):
        result['section_refs'] = _references(data['sections'])
        result['section_count'] = len(data['sections'])
    if isinstance(data.get('tables'), list):
        result['tables'] = []
        result['table_count'] = len(data['tables'])
        for table in data['tables'][:_LIMIT]:
            if not isinstance(table, dict):
                continue
            summary = _fields(table, ('source_id', 'schema', 'name', 'qualified_name',
                                     'tool_table_id', 'sql_table', 'sql_dialect'))
            if isinstance(table.get('columns'), list):
                summary['columns'] = _headers(table['columns'])
                summary['column_count'] = len(table['columns'])
            result['tables'].append(summary)
    return result


def source_result_digest(content, tool_name):
    """Return a smaller structured projection, or None when pruning is unsafe.

    Error feedback stays verbatim. Only compiler-owned integer aggregates are
    retained; arbitrary SQL values (even aliases named 'count') are never copied.
    """
    if tool_name not in SOURCE_TOOLS or not isinstance(content, str):
        return None
    value = decoded_result(content)
    if value is not None and (value.get('status') == 'error' or 'history_compaction' in value):
        return None
    result = {'history_compaction': {
        'kind': 'source_result_digest', 'tool': tool_name,
        'details_omitted': True, 'evidence_status': 'unchanged',
        'note': 'Older result details were removed from this request only. '
                'The original trace is unchanged. Missing details are not negative evidence '
                'and this digest does not validate a candidate. Requery the authorized '
                'source for omitted cells or document text; never reconstruct them.',
    }}
    if value is not None:
        result.update(_fields(value, ('status', 'operator', 'probe_id', 'code')))
        if isinstance(value.get('coverage'), dict):
            result['coverage'] = _fields(value['coverage'], sorted(_COVERAGE))
        result['data'] = _data_digest(value.get('data'))
        aggregates = value.get('aggregates')
        if tool_name in {'profile_columns', 'check_fd', 'check_reference'} and isinstance(aggregates, dict):
            result['aggregates'] = _counts(aggregates)
            bindings = value.get('aggregate_columns', {})
            for name, counts in list(aggregates.items())[:_LIMIT]:
                if not isinstance(name, str) or len(name) > 512 or not isinstance(counts, dict):
                    continue
                result['aggregates'][name] = _counts(counts)
            if isinstance(bindings, dict):
                result['aggregate_columns'] = {name: column for name, column in list(bindings.items())[:_LIMIT]
                                               if isinstance(name, str) and len(name) <= 512
                                               and isinstance(column, str) and len(column) <= 512}
        context = value.get('context')
        if isinstance(context, dict) and isinstance(context.get('document_refs'), list):
            result['document_refs'] = _references(context['document_refs'])
            result['document_reference_count'] = len(context['document_refs'])
    compacted = canonical(result)
    # A shorter JSON string is not necessarily a cheaper model input. Keep the
    # minimum text reclaim, but also require a reduction in estimated tokens.
    return compacted if (len(compacted) + 256 < len(content)
                         and estimate_tokens(compacted) < estimate_tokens(content)) else None
