"""Verify qualified worksheet assertions while preserving every raw relation record."""
from collections import defaultdict

from lightgraph.contracts import canonical, digest
from lightgraph.hierarchy_bindings import mapped_direction
from lightgraph.worksheet_witness import worksheet_row_witness

from .contexts import business_context_identity
from .identity_authority import source_address


def support_address(support):
    return source_address((support.get('provenance') or {}).get('source'))


def interval_union(spans):
    """Compare row sets without expanding individual physical row numbers."""
    merged = []
    for start, end in sorted(spans):
        if merged and merged[-1][1] + 1 >= start:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return tuple(merged)


def support_assertion(support):
    """Every span is keyed by its *own* endpoints and qualifications before combining supports."""
    if not isinstance(support, dict) or not isinstance(support.get('provenance'), dict):
        return 'missing_mapping_provenance', None, None
    prov = support['provenance']
    locator = worksheet_row_witness(support.get('source_row_witness'))
    if locator is None:
        return 'missing_or_invalid_physical_rows', None, None
    address = support_address(support)
    if not address or support.get('inherited_from') or support.get('lookup_matches') or any(
            prov.get(key) for key in ('source_lookup', 'target_lookup', 'metadata_endpoint')):
        return 'unverified_direct_source', None, None
    src, dst = prov.get('source_key_columns'), prov.get('target_key_columns')
    if not all(isinstance(cols, list) and cols and all(isinstance(c, str) and c for c in cols) for cols in (src, dst)):
        return 'missing_endpoint_columns', None, None
    ordinals = prov.get('source_column_ordinals') or {}
    if (not isinstance(ordinals, dict)
            or any(type(v) is not int or not 1 <= v <= 16384 for v in ordinals.values())
            or len(set(ordinals.values())) != len(ordinals) or not set(src + dst) <= ordinals.keys()):
        return 'missing_or_invalid_endpoint_ordinals', None, None
    context = business_context_identity(support)
    scope = support.get('scope_ref')
    scope_columns, required = prov.get('scope_columns'), support.get('required_scope_refs', [])
    mapping = prov.get('business_context_mapping')
    if (not isinstance(scope, str) or not scope or context['status'] == 'unknown'
            or not all(isinstance(seq, list) and all(isinstance(v, str) and v for v in seq) for seq in (scope_columns, required))
            or not isinstance(mapping, dict) or mapping.get('status') != context['status']):
        return 'unknown_access_or_business_scope', None, None
    # The executor omits the predicate for an unfiltered mapping. Incomplete legacy
    # provenance cannot establish that both missing predicates meant unrestricted.
    if not prov.get('definition') or type(prov.get('mapping')) is not int:
        return 'missing_mapping_provenance', None, None
    context_cols = prov.get('context_columns') or []
    row = support.get('row_context') or {}
    if not isinstance(row, dict):
        return 'missing_typed_context_cells', None, None
    values, kinds = row.get('values') or {}, row.get('value_types') or {}
    if (not isinstance(context_cols, list) or not isinstance(values, dict) or not isinstance(kinds, dict)
            or not set(context_cols) <= values.keys() or set(values) != set(kinds)):
        return 'missing_typed_context_cells', None, None
    properties = support.get('properties')
    columns, constants = prov.get('property_columns') or {}, prov.get('constant_properties') or {}
    if not isinstance(columns, dict) or not isinstance(constants, dict):
        return 'unobserved_mapped_property', None, None
    mapped = columns.keys() | constants.keys()
    if not isinstance(properties, dict) or any(not isinstance(properties.get(k), list) or not properties[k]
                                               for k in mapped):
        return 'unobserved_mapped_property', None, None
    if any(canonical(properties[k]) != canonical([v]) for k, v in constants.items()):
        return 'property_value_contradicts_mapping', None, None
    return None, {
        'endpoint_bindings': (address, tuple((c, ordinals[c]) for c in src), tuple((c, ordinals[c]) for c in dst)),
        'scope': (scope, sorted(required), sorted(scope_columns), context, mapping),
        'predicate': prov.get('predicate'),
        'row_context': (sorted(context_cols), values, kinds),
        'properties': (columns, constants, properties),
    }, locator


def assertion_signature(edge, definition):
    if not definition or edge.get('status', 'accepted') != 'accepted':
        return 'missing_accepted_definition', None, {}
    if edge['source'] == edge['target']:
        return 'self_loop_retained', None, {}
    if not edge.get('supports'):
        return 'no_supports', None, {}
    flow = definition.get('hierarchy_orientation')
    mapped = mapped_direction(edge['type'])
    if flow in ('parent_to_child', 'child_to_parent'):
        declared = 'outgoing' if flow == 'parent_to_child' else 'incoming'
        if mapped and mapped != declared:
            return 'conflicting_hierarchy_direction', None, {}
        flow = declared
    elif flow is None:
        flow = mapped or 'directed'
    elif flow == 'none':
        flow = 'directed'
    else:
        return 'unknown_direction', None, {}
    facets = defaultdict(set)
    facets['direction'].add(flow)
    facets['edge_properties'].add(canonical(edge.get('properties') or {}))
    if 'business_context' in edge:
        aggregate_context = business_context_identity(edge)
        if aggregate_context['status'] == 'unknown':
            return 'unknown_aggregate_business_scope', None, {}
        facets['edge_scope'].add(canonical(aggregate_context))
    slices = defaultdict(list)
    for support in edge['supports']:
        reason, qualifiers, locator = support_assertion(support)
        if reason:
            return reason, None, {}
        for key, value in qualifiers.items():
            facets[key].add(canonical(value))
        slices[canonical(qualifiers), locator['snapshot']].extend(locator['rows'])
    records = [(key, interval_union(spans)) for key, spans in sorted(slices.items())]
    facets['located_row_sets'] = {canonical([key[1], rows]) for key, rows in records}
    evidence = {k: digest(sorted(v)) for k, v in facets.items()}
    return None, digest(evidence, records), evidence
