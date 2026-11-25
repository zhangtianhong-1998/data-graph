"""Compare mapping definitions in one extract workspace without materializing instances."""
from collections import Counter, defaultdict

from lightgraph.contracts import digest
from .models import EntityMapping


def _address(mapping):
    return (mapping.source_id, mapping.schema_name, mapping.table)


def _catalog_signatures(tables):
    columns = defaultdict(list)
    for address, table in sorted(tables.items()):
        for column in table.columns:
            columns[column.name].append({'source': list(address), 'data_type': column.data_type})
    return [{'basis': 'shared_column_name', 'column': name, 'member_count': len(peers),
             'members': peers[:8], 'omitted_members': max(0, len(peers) - 8)}
            for name, peers in sorted(columns.items()) if len(peers) > 1]


def _projections(draft, revision, visible_columns):
    roles = defaultdict(set)
    for relation in draft.relations:
        for mapping in relation.mappings:
            address = _address(mapping)
            allowed = visible_columns.get(address)
            if not allowed:
                continue
            for end, opposite in [('source', 'target'), ('target', 'source')]:
                own = getattr(mapping, end + '_key_columns')
                peer = getattr(mapping, opposite + '_key_columns')
                if (getattr(mapping, end + '_lookup') or not own or not peer
                        or not set(own + peer).issubset(allowed)):
                    continue
                key = (getattr(relation, end + '_type'), address, tuple(own))
                roles[key].add((relation.type, end, getattr(relation, opposite + '_type'), tuple(peer)))
    for entity in draft.entities:
        for index, mapping in enumerate(entity.mappings):
            if not isinstance(mapping, EntityMapping):
                continue
            address = _address(mapping)
            allowed = visible_columns.get(address)
            columns = tuple(mapping.key_columns)
            if not allowed or not set(columns).issubset(allowed):
                continue
            label = mapping.label_column if mapping.label_column in allowed else None
            incident = sorted(roles[entity.type, address, columns])
            attributes = sorted((name, column) for name, column in mapping.properties.items()
                                if column in allowed)
            yield {'draft_id': draft.draft_id, 'revision': revision, 'entity_type': entity.type, 'mapping_index': index,
                   'source': list(address), 'key_columns': list(columns), 'label_column': label,
                   'key_names': entity.key_names, 'identity_scope': entity.identity_scope,
                   'identity_context_keys': entity.identity_context_keys,
                   'mapped_properties': dict(attributes[:16]), 'property_count': len(attributes),
                   'identity_domain_digest': digest(entity.identity_domain) if entity.identity_domain else None,
                   'filter_digest': digest(mapping.predicate, mapping.business_context.model_dump(mode='json')),
                   'role_count': len(incident), 'roles': [
                       {'relation_type': rel, 'end': end, 'opposite_type': peer, 'opposite_columns': list(cols)}
                       for rel, end, peer, cols in incident[:8]]}


def inspect_projections(records, tables, context, *, proposed=None, selected=None, offset=0, limit=8):
    """The receipt binds this structural inspection, including exclusions, to the visible drafts."""
    current = sorted((r for r in records if r.context_fingerprint == context and r.status != 'stale'
                      and (proposed is None or r.draft.draft_id != proposed.draft_id)),
                     key=lambda r: r.draft.draft_id)
    drafts = [(r.draft, r.revision, r.fingerprint) for r in current]
    if proposed is not None:
        drafts.append((proposed, None, digest(proposed.model_dump(mode='json', by_alias=True))))
        selected = proposed.draft_id
    catalog = {(t.source_id, t.schema_name, t.name): t for t in tables}
    visible_columns = {address: set(t.column_names) for address, t in catalog.items()}
    receipt = digest('branch_projection_review_v1', context, selected,
                     sorted((d.draft_id, rev, fp) for d, rev, fp in drafts))
    buckets = defaultdict(list)
    for draft, rev, _ in drafts:
        for projection in _projections(draft, rev, visible_columns):
            source = tuple(projection['source'])
            keys = tuple(projection['key_columns'])
            buckets['same_physical_key', source, keys].append(projection)
            buckets['same_physical_key_columns', (), keys].append(projection)
            buckets['same_declared_key_names', (), tuple(projection['key_names'])].append(projection)
    patterns, seen = [], set()
    for basis in ('same_physical_key', 'same_physical_key_columns', 'same_declared_key_names'):
        for (kind, _, _), members in sorted(buckets.items()):
            if kind != basis or len(members) < 2 or selected and not any(m['draft_id'] == selected for m in members):
                continue
            identities = frozenset((m['draft_id'], m['entity_type'], m['mapping_index']) for m in members)
            if identities in seen:
                continue
            seen.add(identities)
            comparison = ('label_column', 'key_names', 'identity_scope', 'identity_context_keys',
                          'identity_domain_digest', 'filter_digest')
            differences = [field for field in comparison if len({digest(m[field]) for m in members}) > 1]
            patterns.append({'basis': basis, 'member_count': len(members),
                             'definition_count': len({(m['draft_id'], m['entity_type']) for m in members}),
                             'members': members[:8], 'omitted_members': max(0, len(members) - 8),
                             'different_fields': differences})
    # Before any mapping exists the Agent can still compare authorized columns and types.
    schema = {}
    if not drafts:
        patterns = _catalog_signatures(catalog)
        schema['tables'] = [{'source': list(address), 'column_count': len(table.columns),
                             'column_signature': digest([(c.name, c.data_type) for c in table.columns])}
                            for address, table in sorted(catalog.items())[:8]]
        schema['omitted_tables'] = max(0, len(catalog) - 8)
    differences = Counter(name for p in patterns for name in p.get('different_fields', ()))
    return {'status': 'ok', 'stage': 'draft_projections' if drafts else 'source_schema',
            'inspection_id': receipt, 'selected_draft': selected, 'draft_count': len(drafts),
            'table_count': len(catalog), 'pattern_count': len(patterns),
            'patterns_by_different_field': dict(differences), 'basis_counts': dict(Counter(
                p['basis'] for p in patterns)), 'patterns': patterns[offset:offset + limit],
            'next_offset': offset + limit if offset + limit < len(patterns) else None, **schema}
