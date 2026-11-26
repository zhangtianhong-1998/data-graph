"""Collect local, source-qualified observations for named frozen input nodes."""
from collections import Counter, defaultdict
import re
from types import SimpleNamespace

from lightgraph.contracts import canonical, digest
from lightgraph.construction.contexts import business_context_identity
from lightgraph.construction.identity_authority import _global_authorities, source_address
from lightgraph.construction.merge_name_audit import _typed_key
from lightgraph.construction.observed_identity import identity_qualifier
from lightgraph.construction.projection_access import _CheckpointGrant, common_table_grant


ADDRESS = ('source_id', 'schema', 'table')
SECTION = ('document_id', 'revision', 'section_id', 'content_digest')


def _pick(row, keys):
    return {k: row[k] for k in keys if k in row}


def _sections(marker):
    return sorted({s for s in marker.get('key_sections_v1', ())
                   if isinstance(s, str) and re.fullmatch('[a-f0-9]{64}', s)})


def _address(prov):
    return _pick(prov.get('source') or {}, ADDRESS)


def _binding(owner, name, source_key, grant, component_hashes):
    source_id, schema, table, columns = source_key
    access = {'status': 'unverified'}
    if grant is not None:
        scope_columns, allowed = grant
        access = {'status': 'checkpoint_table_grant', 'scope_columns': list(scope_columns),
                  'column_selection': 'all' if allowed is None else 'selected',
                  'key_columns_covered': allowed is None or set(columns) <= allowed}
    if component_hashes:
        access['frozen_policy_component_digests'] = component_hashes
    return {'contributor_id': owner, 'entity_type': name, 'source_id': source_id, 'schema': schema,
            'table': table, 'key_columns': list(columns), 'access': access}


def _policy_components(contributor, address):
    witness = contributor.get('_projection_grant')
    if not isinstance(witness, _CheckpointGrant) or witness.fingerprint != contributor['graph'].get('access_fingerprint'):
        return {}
    policy = witness.policy
    source_id, schema, table = address
    sources = [source for source in policy['sources'] if source['id'] == source_id]
    if len(sources) != 1:
        return {}
    source, = sources
    scopes = [b.get('columns') for b in policy['scope_bindings'] if b['source_id'] == source_id
              and b.get('schema', 'public') == schema and b['table'] == table]
    return {**{k: digest(policy.get(k)) for k in ('isolation_domain', 'document_scopes', 'goal', 'inherited_graphs')},
            'source_except_table_and_column_selection': digest({k: v for k, v in source.items()
                                                                if k not in ('allowed_tables', 'allowed_columns')}),
            'source_table_selection': digest(source['allowed_tables']),
            'this_table_column_selection': digest((source['allowed_columns'] or {}).get(schema + '.' + table)),
            'this_table_scope_columns': digest(scopes)}


def _locators(contributor, refs):
    records = contributor.get('evidence_records') or {}
    catalog = contributor['graph'].get('evidence_catalog') or {}
    result = []
    for ref in sorted(refs):
        entry = catalog.get(ref, {})
        record = records.get(ref)
        if not record and entry.get('contributor_id') == contributor['contributor_id']:
            record = records.get(entry.get('original_ref'))
        data = record or entry
        details = data.get('details') or {}
        source = data.get('source_ref')
        row = {'ref': ref, 'kind': data.get('kind', 'unknown'),
               'record_available': bool(record), **_pick(entry, ('original_ref', 'contributor_id')),
               'source': _pick(source, ADDRESS) if isinstance(source, dict) else {},
               **_pick(details, (*SECTION, 'operation', 'complete'))}
        if data.get('kind') == 'document_section' and details.get('complete') and all(details.get(k) for k in SECTION):
            row['versioned_section_id'] = digest('identity_field_section_v1', *(details[k] for k in SECTION))
            row['document_source_tables'] = [_pick(s, ADDRESS) for s in details.get('sources', ()) if isinstance(s, dict)]
        result.append(row)
    return result


def _groups(records):
    grouped = {}
    for record in records:
        key = canonical(record)
        if key not in grouped:
            grouped[key] = {**record, 'support_count': 0}
        grouped[key]['support_count'] += 1
    return [grouped[key] for key in sorted(grouped)]


def _proof(node, definition, support):
    prov = support.get('provenance') or {}
    marker = prov.get('global_identity_authority') or {}
    address = source_address(prov.get('source'))
    cols = prov.get('key_columns') or []
    domain = definition.get('identity_domain')
    bound = bool(domain and address and cols and marker.get('binding') == digest(domain, address, cols))
    recorded = _sections(marker)
    props = support.get('observed_properties', support.get('properties')) or {}
    declared = prov.get('identity_qualifiers') or {}
    qualifiers = [{'property': prop, 'role': role, 'explicit': prop in declared,
                   'column': (prov.get('property_columns') or {}).get(prop), 'values': props.get(prop)}
                  for prop in sorted(set(props) | set(declared)) if (role := identity_qualifier(prov, prop))]
    label = support.get('observed_label') or support.get('label')
    origins = support.get('execution_origins') or [support.get('validation_origin') or {}]
    refs = set(support.get('evidence_refs') or ()) | set(prov.get('evidence_refs') or ())
    for origin in origins:
        refs.update(origin.get('evidence_refs') or ())
    return {**_address(prov), 'key_columns': cols, 'label_column': prov.get('label_column'),
            'qualified_name': label if prov.get('label_column') and isinstance(label, str) and label.strip() else None,
            'scope_ref': support.get('scope_ref'), 'scope_columns': prov.get('scope_columns'),
            'business_context': business_context_identity(support),
            'qualifiers': qualifiers, 'property_columns': prov.get('property_columns') or {},
            'authority': {'marker_present': bool(marker), 'binding_matches': bound,
                          'source_metadata': marker.get('source_metadata') is True,
                          'recorded_section_ids': recorded,
                          'valid_section_ids': recorded if bound and node.get('identity_domain') == domain else []},
            'evidence_refs': sorted(refs), 'mapping': prov.get('mapping'),
            'origins': [_pick(o, ('contributor_id', 'draft_id', 'revision', 'draft_revision', 'validation_id'))
                        for o in origins if o]}


def _roles(incident, node_index, duplicate_ids, entities, relations, limit):
    grouped = {}
    for end, edge in sorted(incident, key=lambda row: (row[1]['id'], row[0])):
        opposite = 'target' if end == 'source' else 'source'
        peer_id = edge[opposite]
        peer = {} if peer_id in duplicate_ids else node_index.get(peer_id, {})
        seen_roles = set()
        for support in edge.get('supports') or [{}]:
            prov = support.get('provenance') or {}
            fields = {'relation_type': edge['type'], 'end': end, **_address(prov),
                      'key_columns': prov.get(end + '_key_columns') or [],
                      'opposite_key_columns': prov.get(opposite + '_key_columns') or []}
            signature = canonical(fields)
            if signature not in grouped:
                relation = relations.get(edge['type'], {})
                orientation = relation.get('hierarchy_orientation')
                category = {'none': 'ordinary', 'parent_to_child': 'hierarchy', 'child_to_parent': 'hierarchy'}.get(
                    orientation, 'undeclared')
                grouped[signature] = {**fields, 'edge_count': 0, 'support_count': 0, 'self_loop_edges': 0,
                                      'declared_contract': _pick(relation, ('source_type', 'target_type',
                                                                            'original_type', 'hierarchy_orientation')),
                                      'declared_relation_category': category,
                                      'identity_role_semantics': 'unverified', 'examples': []}
            group = grouped[signature]
            group['support_count'] += bool(edge.get('supports'))
            if signature in seen_roles:
                continue
            seen_roles.add(signature)
            group['edge_count'] += 1
            group['self_loop_edges'] += edge['source'] == edge['target']
            if len(group['examples']) < limit:
                group['examples'].append({'edge_id': edge['id'], 'peer_id': peer_id, 'peer_type': peer.get('type'),
                                          'peer_status': 'duplicate' if peer_id in duplicate_ids else
                                          'available' if peer else 'missing',
                                          'peer_typed_key': _typed_key(peer, entities.get(peer.get('type'), {})),
                                          'scope_ref': support.get('scope_ref')})
    result = [grouped[k] for k in sorted(grouped)]
    for group in result:
        group['examples_omitted'] = group['edge_count'] - len(group['examples'])
    return result


def collect_member_evidence(contributor, node_ids, *, example_limit=5):
    """Scan each checkpoint once. Key all results by the input node ID within this owner."""
    if example_limit < 0:
        raise ValueError('example_limit must be non-negative')
    owner = contributor['contributor_id']
    requested = set(node_ids)
    if not requested:
        return {}
    graph = contributor['graph']
    entities = {d['type']: d for d in graph.get('definitions', {}).get('entities', ())}
    relations = {d['type']: d for d in graph.get('definitions', {}).get('relations', ())}
    node_index, duplicates = {}, Counter()
    for node in graph.get('nodes', ()):
        node_id = node['id']
        if node_id in node_index:
            duplicates[node_id] += 1
        else:
            node_index[node_id] = node
    incident = defaultdict(list)
    for edge in graph.get('edges', ()):
        for end in ('source', 'target'):
            if edge[end] in requested:
                incident[edge[end]].append((end, edge))
    result = {}
    table_grants = {}
    for node_id in sorted(requested):
        node = node_index.get(node_id)
        record = {'contributor_id': owner, 'node_id': node_id, 'matches': 1 + duplicates[node_id] if node else 0}
        result[node_id] = record
        if not node or node_id in duplicates:
            record.update(status='duplicate' if node else 'missing', bindings=[], roles=[])
            continue
        entity_type = node['type']
        definition = entities.get(entity_type, {})
        proofs = _groups(_proof(node, definition, support) for support in node.get('supports', ()))
        keys = {(*address, tuple(row['key_columns'])) for row in proofs
                if (address := source_address(row)) and row['key_columns']}
        for source_id, schema, table, _ in keys:
            address = source_id, schema, table
            if address not in table_grants:
                witness = common_table_grant(
                    [contributor], SimpleNamespace(source_id=source_id, schema_name=schema, table=table))
                table_grants[address] = witness, _policy_components(contributor, address)
        refs = set(definition.get('identity_evidence_refs') or ())
        for row in proofs:
            refs.update(row['evidence_refs'])
        authorities = _global_authorities(node, definition, {}, local_namespace=False)
        bound_sections = sorted(s for kind, s in authorities if kind == 'document'
                                and isinstance(s, str) and re.fullmatch('[a-f0-9]{64}', s))
        record.update(status='available', entity_type=entity_type, label=node.get('label'),
                      label_variants=node.get('label_variants', []), label_conflict=bool(node.get('label_conflict')),
                      identity=node.get('identity'), identity_types=node.get('identity_types'),
                      typed_key=_typed_key(node, definition), node_identity=_pick(node, ('identity_scope',
                          'identity_origin', 'identity_domain', 'identity_context')),
                      definition=_pick(definition, ('type', 'original_type', 'key_names', 'identity_origin',
                          'identity_scope', 'identity_domain', 'identity_context_keys', 'identity_evidence_refs')),
                      bindings=[_binding(owner, entity_type, k, *table_grants[k[:3]]) for k in sorted(keys)],
                      support_count=len(node.get('supports', ())), support_groups=proofs,
                      access_fingerprint=graph.get('access_fingerprint'),
                      common_bound_sections=bound_sections, global_scope_authority_sections=(
                          bound_sections if definition.get('identity_scope') == 'global' else []),
                      evidence_locators=_locators(contributor, refs),
                      roles=_roles(incident[node_id], node_index, duplicates, entities, relations, example_limit))
    return result
