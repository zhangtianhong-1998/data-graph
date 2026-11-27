"""Bind explicit identity rules to approved tables and complete physical keys."""
from lightgraph.contracts import canonical
from lightgraph.errors import LightGraphError
from lightgraph.source_values import persisted_identity_key


def allowed_bindings(rule):
    return {(b.source_id, b.schema_name, b.table, tuple(sorted(b.key_columns.items()))) for b in rule.source_bindings}


def matches_binding(allowed, source, names, columns):
    if (not isinstance(source, dict) or not names or not isinstance(columns, list)
            or len(columns) != len(names) or not all(isinstance(k, str) and k for k in columns)):
        return False
    address = (source.get('source_id'), source.get('schema'), source.get('table'))
    return all(isinstance(k, str) and k for k in address) and (
        *address, tuple(sorted(zip(names, columns, strict=True)))) in allowed


def draft_binding_issues(entity, rule):
    allowed = allowed_bindings(rule)
    missing = [i for i, m in enumerate(entity.mappings) if not matches_binding(
        allowed, {'source_id': m.source_id, 'schema': m.schema_name, 'table': m.table}, entity.key_names, m.key_columns)]
    return ([{'code': 'fusion_source_binding_unverified', 'entity_type': entity.type, 'mappings': missing,
              'guidance': 'Confirm each complete table and key-column mapping or use a separate draft type.'}]
            if missing else [])


def _original_key_order(origin, support, columns):
    names = origin.get('source_key_names')
    identity = origin['identity']
    if names is not None:
        return names if (isinstance(names, list) and all(isinstance(n, str) for n in names)
                         and len(names) == len(identity) and set(names) == identity.keys()) else []
    if len(identity) == 1:
        return list(identity)
    # Historical explicit fusions recorded physical column -> raw value,
    # but did not record the logical key order. Equal components are ambiguous.
    values = support.get('original_key_values') or {}
    if not isinstance(values, dict) or not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
        return []
    by_value = {canonical(value): name for name, value in identity.items()}
    if len(by_value) != len(identity) or set(values) != set(columns):
        return []
    names = [by_value.get(canonical(values[c])) for c in columns]
    return names if set(names) == identity.keys() else []


def validate_source_bindings(graph, rules, previous_targets, *, local_namespace):
    from .fusion import normalize_key_value
    approved = {key: allowed_bindings(rule) for key, rule in rules.items() if rule.source_bindings}
    if not approved:
        return
    targets = {(rules[k].canonical_type, rules[k].identity_domain) for k in approved}
    definitions = {d['type']: d['key_names'] for d in graph.get('definitions', {}).get('entities', [])}
    for node in graph.get('nodes', []):
        key = (node['type'], frozenset(node.get('identity', {})))
        target = (node['type'], node.get('identity_domain'))
        if target not in targets:
            target = (node.get('original_type', node['type']), node.get('identity_domain'))
        prepared = target in targets and target[0] in previous_targets
        if key not in approved and not prepared:
            if target in targets and key not in rules:
                raise LightGraphError('fusion_source_binding_unverified')
            continue
        if not local_namespace or not node.get('supports'):
            raise LightGraphError('fusion_source_binding_unverified')
        for support in node['supports']:
            provenance = support.get('provenance') or {}
            source = provenance.get('source') or {}
            columns = provenance.get('key_columns') or []
            if support.get('inherited_from'):
                raise LightGraphError('fusion_source_binding_unverified')
            if not prepared:
                names = definitions.get(node['type'])
                if support.get('identity_origins') or not matches_binding(approved[key], source, names, columns):
                    raise LightGraphError('fusion_source_binding_unverified')
                continue
            # An earlier policy stamp identifies a canonical type; only the raw
            # identities on each support identify which table rule produced it.
            origins = support.get('identity_origins') or []
            if not isinstance(origins, list) or not origins:
                raise LightGraphError('fusion_source_binding_unverified')
            for origin in origins:
                if not isinstance(origin, dict) or not isinstance(origin.get('identity'), dict):
                    raise LightGraphError('fusion_source_binding_unverified')
                identity = origin.get('identity') or {}
                old_key = (origin.get('type'), frozenset(identity))
                rule = rules.get(old_key)
                if not rule or (rule.canonical_type, rule.identity_domain) != target:
                    raise LightGraphError('fusion_source_binding_unverified')
                normalized = {'identity': {k: normalize_key_value(identity[v], rule.key_normalizers.get(k, ()),
                                                                  rule.key_prefix_strips.get(k, ()))
                                           for k, v in rule.key_mapping.items()},
                              'identity_types': {k: origin['identity_types'][v] for k, v in rule.key_mapping.items()
                                                 if v in origin.get('identity_types', {})}}
                if persisted_identity_key(normalized) != persisted_identity_key(node):
                    raise LightGraphError('fusion_source_binding_unverified')
                if old_key in approved:
                    names = _original_key_order(origin, support, columns)
                    if not matches_binding(approved[old_key], source, names, columns):
                        raise LightGraphError('fusion_source_binding_unverified')
                    if 'original_key_values' in support:
                        values = {col: identity[n] for n, col in zip(names, columns, strict=True)}
                        if canonical(values) != canonical(support['original_key_values']):
                            raise LightGraphError('fusion_source_binding_unverified')
