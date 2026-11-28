"""Per-extract structural index for bounded, pattern-level fusion reviews."""
from collections import defaultdict

from lightgraph.contracts import digest
from .fusion import definition_contract
from .source_namespaces import BINDING


def _filter(keys):
    # A disjoint bitmap proves disjoint keys. A shared bit only selects a page
    # for exact comparison, never a rule. Hashes are the typed-key digests.
    bits = 0
    for key in keys:
        bits |= 1 << (int(key[:4], 16) % 8192)
    return format(bits, 'x')


def possibly_overlapping(entries, field):
    entries = [(key, int(row[field], 16)) for key, row in entries]
    selected = set()
    for ordered in (entries, reversed(entries)):
        previous = 0
        for key, bits in ordered:
            if bits & previous:
                selected.add(key)
            previous |= bits
    return selected


def _binding(raw):
    source, schema, table, columns = raw
    return source, schema, table, tuple(columns)


def relation_binding(raw):
    source, schema, table, left, right = raw
    return source, schema, table, tuple(left), tuple(right)


def overlap_identity_contract(definition):
    contract = definition_contract(definition)
    for field in ('type', 'key_names'):
        contract.pop(field, None)
    return contract


def _index_role_descriptors(role_descriptors, *, per_binding=16, max_total=128):
    ordered = sorted(role_descriptors)
    if len(ordered) <= per_binding:
        return ordered
    by_columns = defaultdict(list)
    for item in ordered:
        by_columns[item[2]].append(item)
    groups = [items[:per_binding] for _, items in sorted(by_columns.items())]
    selected = []
    for depth in range(per_binding):
        for group in groups:
            if depth < len(group):
                selected.append(group[depth])
                if len(selected) >= max_total:
                    return sorted(selected)
    return sorted(selected)


def _family_row(f):
    contract = overlap_identity_contract(f.definition)
    bindings = sorted(f.by_source)
    catalog = sorted(f.by_catalog)
    aliases = [[cell, obs[0], obs[4]] for cell, obs in sorted(f.observations.items()) if obs[3]]
    definition = {k: f.definition[k] for k in ('key_names', 'identity_domain', 'identity_origin', 'identity_scope')
                  if k in f.definition}
    # These fields are needed only as disqualifiers; their contents need not be stored.
    definition.update({k: True for k in ('identity_context_keys', 'definition_variant', 'original_type') if f.definition.get(k)})
    return {
        'type': f.name, 'definition': definition, 'contract_fingerprint': digest(contract),
        'sources': bindings, 'catalog': catalog, 'key_count': len(f.observations),
        'key_filter': _filter(f.observations), 'alias_filter': _filter(digest(cell, label) for cell, label, _ in aliases),
        'witness_count': sum(len(peers) for roles in f.member_roles.values() for peers in roles.values()),
        'problems': sorted(f.problems), 'property_names': sorted(f.property_names)[:32],
        'role_descriptors': _index_role_descriptors(f.role_descriptors),
        'aliases': aliases,
        'evidence': {
            **({'source_registries': [sorted(f.source_registries.get(b, ())) for b in bindings]}
               if any(f.source_registries.values()) else {}),
            'observations': {cell: list(obs[:3]) for cell, obs in f.observations.items()},
            'source_keys': [sorted(f.by_source[b]) for b in bindings],
            'catalog_keys': [sorted(f.by_catalog[b]) for b in catalog],
            'unique_keys': sorted(f.dictionary_members), 'foreign_roles': sorted(f.foreign_roles),
            'roles': sorted(f.roles),
            'member_roles': {cell: [[list(role), sorted(peers)] for role, peers in sorted(roles.items())]
                             for cell, roles in f.member_roles.items()},
        },
    }


def family_header(owner, row):
    from .overlap_patterns import _Family
    f = _Family(owner, row['type'], row['definition'], row['contract_fingerprint'])
    f.by_source = {_binding(b): () for b in row['sources']}
    f.by_catalog = {_binding(b): () for b in row['catalog']}
    f.problems = set(row['problems'])
    f.property_names = set(row['property_names'])
    f.role_descriptors = {(relation, end, tuple(columns), tuple(peer))
                          for relation, end, columns, peer in row['role_descriptors']}
    return f


def load_family_evidence(f, evidence):
    f.observations = evidence['observations']
    f.by_source = {b: set(keys) for b, keys in zip(f.by_source, evidence['source_keys'], strict=True)}
    if 'source_registries' in evidence:
        f.source_registries = {b: set(tokens) for b, tokens in zip(f.by_source, evidence['source_registries'], strict=True)}
    f.by_catalog = {b: set(keys) for b, keys in zip(f.by_catalog, evidence['catalog_keys'], strict=True)}
    f.dictionary_members = set(evidence['unique_keys'])
    f.foreign_roles = {tuple(cols) for cols in evidence['foreign_roles']}
    f.roles = {tuple(role) for role in evidence['roles']}
    f.member_roles = {cell: {tuple(role): set(peers) for role, peers in roles}
                      for cell, roles in evidence['member_roles'].items()}


def build_index(contributor):
    from .overlap_patterns import MAX_DEFINITIONS, MAX_RECORDS, MAX_SUPPORTS

    owner, graph = contributor['contributor_id'], contributor['graph']
    definitions = graph.get('definitions', {})
    nodes, edges = graph.get('nodes', ()), graph.get('edges', ())
    counts = {'definitions': sum(len(definitions.get(name, ())) for name in ('entities', 'relations')),
              'records': len(nodes) + len(edges), 'supports': 0}
    result = {'format_version': 1, 'contributor_id': owner, 'access_fingerprint': graph.get('access_fingerprint'),
              'namespace_binding': graph.get(BINDING), 'extends_base_graph': bool(graph.get('extends_base_graph')),
              'counts': counts, 'status': 'analysis_budget_exceeded', 'type_names': [], 'families': [], 'relations': []}
    if counts['definitions'] > MAX_DEFINITIONS or counts['records'] > MAX_RECORDS:
        return result
    counts['supports'] = sum(len(item.get('supports', ())) for bucket in (nodes, edges) for item in bucket)
    if counts['supports'] > MAX_SUPPORTS:
        return result
    try:
        return _capture(owner, graph, definitions, nodes, edges, result)
    except (KeyError, TypeError, ValueError, AttributeError):
        # Legacy graph records may predate typed node/edge contracts. Such a
        # graph remains publishable but cannot authorize new identity rules.
        return {**result, 'status': 'unavailable', 'families': [], 'relations': [], 'type_names': []}


def _capture(owner, graph, definitions, nodes, edges, result):
    from .overlap_patterns import _Family, _columns, _read_edges, _read_node
    families, identities, names = {}, {}, set()
    for d in definitions.get('entities', []):
        name = d['type']
        f = _Family(owner, name, d)
        if (owner, name) in families or not _columns(d.get('key_names')):
            f.problems.add('unverified_identity_contract')
        names.add(name)
        families[owner, name] = f
    for node in nodes:
        names.add(node['type'])
        f = families.get((owner, node['type']))
        if f:
            cell = _read_node(f, node)
            if cell:
                identities[node['id']] = cell
    relations = defaultdict(lambda: defaultdict(set))
    _read_edges(owner, graph, families, identities, relations)
    result['type_names'] = sorted(names)
    result['families'] = [_family_row(f) for _, f in sorted(families.items())]
    for binding, group in sorted(relations.items()):
        for (_, name), pairs in sorted(group.items()):
            result['relations'].append({'binding': binding, 'type': name,
                                        'key_filter': _filter(digest(pair) for pair in pairs), 'pair_count': len(pairs),
                                        'evidence': sorted(pairs)})
    result['status'] = 'complete'
    return result
