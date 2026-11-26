"""Verify qualified worksheet assertions while preserving every raw relation record."""
from collections import defaultdict

from lightgraph.contracts import canonical, digest
from lightgraph.hierarchy_bindings import mapped_direction
from lightgraph.worksheet_witness import worksheet_row_witness
from lightgraph.projection_witness import source_projection_witness

from .contexts import business_context_identity
from .identity_authority import source_address


def support_address(support):
    return source_address((support.get('provenance') or {}).get('source'))


def support_locator(support):
    return (worksheet_row_witness(support.get('source_row_witness'))
            or source_projection_witness(support.get('source_projection_witness')))


def interval_union(spans):
    """Compare row sets without expanding individual physical row numbers."""
    merged = []
    for start, end in sorted(spans):
        if merged and merged[-1][1] + 1 >= start:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return tuple(merged)


def support_assertion_checks(support):
    """Report independent blockers; a missing locator must not hide unknown scope."""
    checks = []

    def check(name, condition, reason, *, applicable=True):
        checks.append({'check': name, 'status': ('passed' if condition else 'failed') if applicable else 'not_checked',
                       **({'reason': reason} if not condition else {})})

    valid = isinstance(support, dict) and isinstance(support.get('provenance'), dict)
    check('provenance', valid, 'missing_mapping_provenance')
    if not valid:
        for name in ('locator', 'direct_source', 'endpoint_columns', 'scope', 'mapping', 'context_cells', 'properties'):
            check(name, False, 'missing_mapping_provenance', applicable=False)
    else:
        prov = support['provenance']
        locator = support_locator(support)
        check('locator', locator is not None, 'missing_or_invalid_physical_rows')
        indirect = bool(support.get('lookup_matches') or prov.get('source_lookup') or prov.get('target_lookup'))
        inherited = bool(support.get('inherited_from') or any(
            m.get('inherited_node_id') for m in support.get('lookup_matches', []) if isinstance(m, dict)))
        reason = ('inherited_fact_equivalence_unverified' if inherited else
                  'lookup_fact_equivalence_unverified' if indirect else 'unverified_direct_source')
        check('direct_source', bool(support_address(support)) and not inherited and not indirect
              and not prov.get('metadata_endpoint'), reason)
        if indirect:
            matches = support.get('lookup_matches') or []
            chain_present = bool(matches) and all(isinstance(m, dict) and (
                source_projection_witness(m.get('source_projection_witness')) or
                isinstance(m.get('inherited_observation_witness'), dict)) for m in matches)
            check('lookup_observation_witness_presence', chain_present, 'missing_lookup_observation_witness')
            checks.append({'check': 'lookup_fact_equivalence', 'status': 'not_checked',
                           'reason': 'lookup_fact_equivalence_unverified'})
        src, dst = prov.get('source_key_columns'), prov.get('target_key_columns')
        columns_valid = all(isinstance(cols, list) and cols and all(isinstance(c, str) and c for c in cols)
                            for cols in (src, dst))
        check('endpoint_columns', columns_valid, 'missing_endpoint_columns')
        ordinals = prov.get('source_column_ordinals') or {}
        if locator and locator['kind'] == 'source_projection_v1':
            declared, scope_cols, context_cols = (prov.get(name, []) for name in
                                                 ('columns', 'scope_columns', 'context_columns'))
            properties = prov.get('property_columns', {})
            mapping = prov.get('business_context_mapping')
            mapped = mapping.get('columns', {}) if isinstance(mapping, dict) else None
            valid = (columns_valid and all(isinstance(cols, list)
                     and all(isinstance(c, str) and c for c in cols)
                     for cols in (declared, scope_cols, context_cols))
                     and all(isinstance(cols, dict) and all(isinstance(k, str) and k
                         and isinstance(c, str) and c for k, c in cols.items()) for cols in (properties, mapped)))
            if valid:
                used = set(src + dst + context_cols + list(properties.values()) + list(mapped.values()))
                valid = used <= set(declared) and set(declared + scope_cols) <= set(locator['columns'])
            check('projection_columns', valid, 'missing_or_invalid_projection_columns')
        else:
            check('endpoint_ordinals', isinstance(ordinals, dict) and columns_valid
                  and all(type(v) is int and 1 <= v <= 16384 for v in ordinals.values())
                  and len(set(ordinals.values())) == len(ordinals)
                  and set((src or []) + (dst or [])) <= ordinals.keys(), 'missing_or_invalid_endpoint_ordinals')
        context = business_context_identity(support)
        scope = support.get('scope_ref')
        scope_columns, required = prov.get('scope_columns'), support.get('required_scope_refs', [])
        mapping = prov.get('business_context_mapping')
        check('scope', isinstance(scope, str) and bool(scope) and context['status'] != 'unknown'
              and all(isinstance(seq, list) and all(isinstance(v, str) and v for v in seq)
                      for seq in (scope_columns, required))
              and isinstance(mapping, dict) and mapping.get('status') == context['status'],
              'unknown_access_or_business_scope')
        check('mapping', bool(prov.get('definition')) and type(prov.get('mapping')) is int,
              'missing_mapping_provenance')
        context_cols, row = prov.get('context_columns') or [], support.get('row_context') or {}
        values, kinds = (row.get('values') or {}, row.get('value_types') or {}) if isinstance(row, dict) else (None, None)
        check('context_cells', isinstance(context_cols, list)
              and all(isinstance(c, str) and c for c in context_cols)
              and isinstance(values, dict) and isinstance(kinds, dict)
              and set(context_cols) <= values.keys() and set(values) == set(kinds), 'missing_typed_context_cells')
        properties = support.get('properties')
        columns, constants = prov.get('property_columns') or {}, prov.get('constant_properties') or {}
        observed = (isinstance(columns, dict) and isinstance(constants, dict) and isinstance(properties, dict)
                    and all(isinstance(properties.get(k), list) and properties[k] for k in columns.keys() | constants.keys()))
        check('properties', observed, 'unobserved_mapped_property')
        check('constant_properties', observed and all(canonical(properties[k]) == canonical([v])
              for k, v in constants.items()), 'property_value_contradicts_mapping', applicable=observed)
    blockers = list(dict.fromkeys(c['reason'] for c in checks if c['status'] == 'failed'))
    return {'checks': checks, 'blockers': blockers, 'first_failure': blockers[0] if blockers else None,
            'witness_kind': support_locator(support)['kind'] if valid and support_locator(support) else None}


def support_assertion(support):
    """Every span is keyed by its *own* endpoints and qualifications before combining supports."""
    checks = support_assertion_checks(support)
    if checks['first_failure']:
        return checks['first_failure'], None, None
    prov = support['provenance']
    locator = support_locator(support)
    address = support_address(support)
    src, dst = prov['source_key_columns'], prov['target_key_columns']
    ordinals = prov.get('source_column_ordinals') or {}
    context = business_context_identity(support)
    scope, scope_columns = support['scope_ref'], prov['scope_columns']
    required, mapping = support.get('required_scope_refs', []), prov['business_context_mapping']
    context_cols = prov.get('context_columns') or []
    row = support.get('row_context') or {}
    values, kinds = row.get('values') or {}, row.get('value_types') or {}
    properties = support['properties']
    columns, constants = prov.get('property_columns') or {}, prov.get('constant_properties') or {}
    return None, {
        'endpoint_bindings': (address, tuple((c, ordinals.get(c)) for c in src), tuple((c, ordinals.get(c)) for c in dst)),
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
    tuples = defaultdict(set)
    for support in edge['supports']:
        reason, qualifiers, locator = support_assertion(support)
        if reason:
            return reason, None, {}
        for key, value in qualifiers.items():
            facets[key].add(canonical(value))
        if locator['kind'] == 'excel_rows_v1':
            slices[canonical(qualifiers), locator['snapshot']].extend(locator['rows'])
        else:
            tuples[canonical(qualifiers), locator['source_binding'], locator['projection']].add(locator['tuple'])
    records = [(key, interval_union(spans)) for key, spans in sorted(slices.items())]
    facets['located_row_sets'] = {canonical([key[1], rows]) for key, rows in records}
    tuple_records = [(key, sorted(values)) for key, values in sorted(tuples.items())]
    if tuple_records:
        facets['located_projection_tuples'] = {canonical([key[1:], values]) for key, values in tuple_records}
        records.extend(tuple_records)
    evidence = {k: digest(sorted(v)) for k, v in facets.items()}
    return None, digest(evidence, records), evidence
