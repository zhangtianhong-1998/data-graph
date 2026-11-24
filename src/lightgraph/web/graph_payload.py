"""Bound canvas transport; detailed properties remain in the published graph."""
from lightgraph.construction.hierarchy_observations import edge_observation_properties, observation_boundary
from lightgraph.contracts import canonical, digest
from lightgraph.hierarchy_bindings import bound_level_positions, mapped_direction, observed_level_pairs
from lightgraph.worksheet_witness import worksheet_row_witness

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


def relation_definitions(graph):
    return {item['type']: item for item in graph.get('definitions', {}).get('relations', [])}


def hierarchy_display(edge, definitions):
    """Group drawn lines only. The actual edges, their IDs and their supports stay separate."""
    relation = edge.get('relation') or edge.get('type')
    definition = definitions.get(relation)
    if not definition or edge.get('status', 'accepted') != 'accepted':
        return {}
    declared = definition.get('hierarchy_orientation')
    if declared not in (None, 'parent_to_child', 'child_to_parent'):
        return {}
    supports = edge.get('supports') or []
    pairs = observed_level_pairs(relation, supports)
    flow = {'parent_to_child': 'outgoing', 'child_to_parent': 'incoming'}.get(declared)
    if not flow and not pairs:
        return {}
    mapped = mapped_direction(relation)
    result = {'hierarchy_direction': flow or mapped}
    if not pairs or flow and flow != mapped:
        return result
    slots = set()
    orientation = 'parent_to_child' if mapped == 'outgoing' else 'child_to_parent'
    for (address, src, dst), support in zip(pairs, supports):
        witness = worksheet_row_witness(support.get('source_row_witness'))
        if 'source_row_witness' in support and witness is None:
            return {'hierarchy_direction': flow} if flow else {}
        if edge['source'] == edge['target']:
            continue
        try:
            prov = support['provenance']
            positions = bound_level_positions(src, dst, prov.get('context_columns') or ())
            boundary = observation_boundary(support, [src, dst], positions)
            if boundary is None:
                return result
            properties = edge_observation_properties(support, ((src,), (dst,)), positions, orientation)
            ordinals = prov.get('source_column_ordinals') or {}
            slots.add(canonical([address, src, dst, ordinals.get(src), ordinals.get(dst), boundary, properties, witness]))
        except (AttributeError, KeyError, TypeError, ValueError):
            # Unsupported or incomplete legacy evidence can still be inspected
            # as its own relation with all original supports.
            return result
    if slots:
        result['hierarchy_line_key'] = digest(sorted(slots))
    return result


def canvas_item(item, definitions=None):
    value = listed(item)
    if definitions:
        value.update(hierarchy_display(item, definitions))
    if len(canonical(value).encode('utf-8')) <= MAX_ITEM_BYTES:
        return value
    # IDs and endpoints are never shortened. Preview values are explicitly
    # marked and cannot replace the full record returned by the detail API.
    fixed = {'id', 'source', 'target', 'type', 'kind', 'relation', 'layer', 'status', 'label_origin',
             'hierarchy_direction', 'hierarchy_line_key'}
    result = {key: val for key, val in value.items() if key in fixed}
    for key in ('label', 'display_label', 'identity', 'aliases', 'display_values', 'scope_refs'):
        if key in value:
            result[key] = _preview(value[key])
    result.update(overview_only=True, detail_fields_deferred=True)
    if len(canonical(result).encode('utf-8')) > MAX_ITEM_BYTES:
        result = {key: val for key, val in result.items() if key in fixed | {'label', 'display_label', 'overview_only', 'detail_fields_deferred'}}
    return result


def canvas_page(items, offset, limit, *, byte_limit=MAX_PAGE_BYTES, definitions=None):
    page, used = [], 0
    for item in items[offset:offset + limit]:
        value = canvas_item(item, definitions)
        size = len(canonical(value).encode('utf-8')) + 1
        if page and used + size > byte_limit:
            break
        page.append(value)
        used += size
    cursor = offset + len(page)
    return page, cursor if cursor < len(items) else None, used
