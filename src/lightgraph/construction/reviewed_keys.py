"""Apply finite business approvals to the exact checkpoints named by the reviewer."""
from collections import Counter, defaultdict

from lightgraph.contracts import canonical, digest
from lightgraph.source_values import identity_cell
from .contexts import business_context_identity
from .graph_storage import _graph_digest
from .observed_identity import contradictory_qualifiers


def reviewed_observation_digest(kind, value, name):
    """Fingerprint one exact source value, its driver type and its observed name."""
    return digest('reviewed_key_name_v1', kind, value, name)


def _physical_code(node, binding):
    """Count competing same-code rows on this axis, including rows with missing names."""
    identity = node.get('identity') or {}
    if len(identity) != 1:
        return None
    key, = identity
    address = (binding.source_id, binding.schema_name, binding.table)
    for support in node.get('supports', []):
        prov = support.get('provenance') or {}
        source = prov.get('source') or {}
        if (support.get('contributor_id') == binding.contributor_id and prov.get('key_columns') == [binding.key_column]
                and tuple(source.get(k) for k in ('source_id', 'schema', 'table')) == address):
            return canonical([node.get('identity_types', {}).get(key), identity[key]])
    return None


def _observations(node, definition, binding):
    names = definition.get('key_names') or []
    if (len(names) != 1 or set(names) != node.get('identity', {}).keys()
            or set(names) != node.get('identity_types', {}).keys()
            or definition.get('identity_origin', 'row') != 'row'
            or node.get('identity_origin', 'row') != 'row'
            or set(definition.get('identity_context_keys') or []) - set(names)):
        return None
    key, = names
    value = node['identity'][key]
    kind = node['identity_types'][key]
    if value is None or value == '' or identity_cell(node, key)[0] != kind:
        return None
    addresses = (binding.source_id, binding.schema_name, binding.table)
    observations = set()
    for support in node.get('supports', []):
        prov = support.get('provenance') or {}
        source = prov.get('source') or {}
        ctx = support.get('business_context') or {}
        label = support.get('observed_label') or support.get('label')
        if (support.get('contributor_id') != binding.contributor_id or not support.get('scope_ref')
                or support.get('inherited_from') or support.get('identity_basis') or support.get('lookup_matches')
                or support.get('identity_decision_refs')
                or any(o.get('contributor_id') != binding.contributor_id for o in support.get('execution_origins', []))
                or tuple(source.get(k) for k in ('source_id', 'schema', 'table')) != addresses
                or prov.get('key_columns') != [binding.key_column] or prov.get('label_column') != binding.name_column
                or not isinstance(label, str) or not label.strip() or ctx.get('missing_dimensions') or ctx.get('conflicts')):
            return None
        if 'original_key_values' in support:
            if canonical(support['original_key_values']) != canonical({binding.key_column: value}):
                return None
            if any(origin.get('identity_types') and list(origin['identity_types'].values()) != [kind]
                   for origin in support.get('identity_origins') or []):
                return None
        facts = business_context_identity(support)
        dims = facts['dimensions']
        # Only the identity column itself can be redundant. A tenant, date,
        # differing driver type or other business dimension still distinguishes the rows.
        if dims and (set(dims) != {key} or canonical(dims[key]) != canonical(value)
                     or facts['value_types'].get(key) != kind):
            return None
        if definition.get('identity_scope') == 'scoped' and not dims:
            return None
        row = support.get('row_context') or {}
        row_values = row.get('values') or {}
        if ((binding.key_column in row_values and
             (canonical(row_values[binding.key_column]) != canonical(value)
              or (row.get('value_types') or {}).get(binding.key_column) != kind))
                or (binding.name_column in row_values and row_values[binding.name_column] != label)):
            return None
        audience = tuple(sorted({support['scope_ref'], *support.get('required_scope_refs', [])}))
        observations.add((reviewed_observation_digest(kind, value, label), audience, node.get('node_role')))
    return observations.pop() if len(observations) == 1 else None


def reviewed_key_groups(graph, contributors, rules, remaps):
    """Return disjoint approved groups. No family, prefix or transitive authorization."""
    graphs = {c['contributor_id']: c['graph'] for c in contributors if c.get('kind', 'unit') == 'unit'}
    original_nodes = defaultdict(list)
    for cid, kind, _, ids in remaps:
        if cid in graphs and kind == 'unit':
            for node in graphs[cid]['nodes']:
                if node['id'] in ids:
                    original_nodes[cid, ids[node['id']]].append(node['id'])
    digests = {}
    by_family = defaultdict(list)
    by_id = {n['id']: n for n in graph['nodes']}
    for node in graph['nodes']:
        by_family[node.get('original_type', node['type'])].append(node)
    definitions = {d['type']: d for d in graph['definitions']['entities']}
    proposals = []
    stats = Counter(requested_pairs=sum(len(rule.observation_digests) for rule in rules))
    incomplete = {c['contributor_id'] for c in (graph.get('conflict_isolation') or {}).get('contributors_with_gaps', [])}
    for rule in rules:
        if any(p.contributor_id in incomplete for p in rule.projections):
            stats['incomplete_input_rules'] += 1
            continue
        for p in rule.projections:
            if p.contributor_id in graphs and p.contributor_id not in digests:
                digests[p.contributor_id] = _graph_digest(graphs[p.contributor_id])
        if any(digests.get(p.contributor_id) != p.graph_digest for p in rule.projections):
            stats['checkpoint_mismatch_rules'] += 1
            continue
        approved = set(rule.observation_digests)
        decision_id = 'reviewed_keys_' + digest(rule.model_dump(mode='json', by_alias=True))
        by_observation = defaultdict(lambda: defaultdict(list))
        for p in rule.projections:
            peers = by_family[rule.entity_type]
            codes = [_physical_code(node, p) for node in peers]
            occurrences = Counter(codes)
            for node, code in zip(peers, codes, strict=True):
                if code is None or occurrences[code] != 1:
                    continue
                observed = _observations(node, definitions[node['type']], p)
                if observed and observed[0] in approved and len(original_nodes[p.contributor_id, node['id']]) == 1:
                    by_observation[observed][p.contributor_id].append(node['id'])
        for sides in by_observation.values():
            if len(sides) == 2 and all(len(ids) == 1 for ids in sides.values()):
                members = {ids[0]: original_nodes[cid, ids[0]][0] for cid, ids in sides.items()}
                if not contradictory_qualifiers(by_id[n] for n in members):
                    proposals.append((tuple(sorted(members)), (rule.approval_ref, decision_id, members)))
                else:
                    stats['attribute_conflicts'] += 1
    owners = Counter(n for ids, _ in proposals for n in ids)
    groups = {ids: ref for ids, ref in proposals if all(owners[n] == 1 for n in ids)}
    stats['eligible_pairs'] = len(groups)
    return groups, stats
