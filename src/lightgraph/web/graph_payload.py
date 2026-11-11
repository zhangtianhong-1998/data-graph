"""Bound canvas transport; detailed properties remain in the published graph."""
from lightgraph.contracts import canonical

MAX_PAGE_BYTES = 1024 * 1024
MAX_ITEM_BYTES = 32 * 1024


def listed(item):
    return {key: value for key, value in item.items() if key not in {'supports', 'provenance'}}


def _preview(value, depth=0):
    if isinstance(value, str):
        return value if len(value) <= 256 else value[:256] + '…'
    if depth >= 2 and isinstance(value, (dict, list)):
        return '…'
    if isinstance(value, dict):
        return {str(key)[:80]: _preview(val, depth + 1) for key, val in list(value.items())[:8]}
    if isinstance(value, list):
        return [_preview(val, depth + 1) for val in value[:8]]
    return value


def canvas_item(item):
    value = listed(item)
    if len(canonical(value).encode('utf-8')) <= MAX_ITEM_BYTES:
        return value
    # IDs and endpoints are never shortened. Preview values are explicitly
    # marked and cannot replace the full record returned by the detail API.
    fixed = {'id', 'source', 'target', 'type', 'kind', 'relation', 'layer', 'status', 'label_origin'}
    result = {key: val for key, val in value.items() if key in fixed}
    for key in ('label', 'display_label', 'identity', 'aliases', 'display_values', 'scope_refs'):
        if key in value:
            result[key] = _preview(value[key])
    result.update(overview_only=True, detail_fields_deferred=True)
    if len(canonical(result).encode('utf-8')) > MAX_ITEM_BYTES:
        result = {key: val for key, val in result.items() if key in fixed | {'label', 'display_label', 'overview_only', 'detail_fields_deferred'}}
    return result


def canvas_page(items, offset, limit, *, byte_limit=MAX_PAGE_BYTES):
    page, used = [], 0
    for item in items[offset:offset + limit]:
        value = canvas_item(item)
        size = len(canonical(value).encode('utf-8')) + 1
        if page and used + size > byte_limit:
            break
        page.append(value)
        used += size
    cursor = offset + len(page)
    return page, cursor if cursor < len(items) else None, used
