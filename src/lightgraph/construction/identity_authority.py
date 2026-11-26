"""Evidence required to reuse a row identity across independent publications."""
from collections import defaultdict
import re

from lightgraph.contracts import digest
from lightgraph.source_values import persisted_identity_key
from .observed_identity import _dictionary_binding, _node_evidence, _relation_roles, contradictory_qualifiers


def source_address(source):
    if not isinstance(source, dict):
        return None
    parts = tuple(source.get(name) for name in ('source_id', 'schema', 'table'))
    return parts if all(isinstance(p, str) and p for p in parts) else None


def metadata_probe(record):
    details = record.get('details') or {}
    if (record.get('kind') == 'source_probe' and details.get('operation') == 'get_metadata'
            and details.get('complete') is True):
        return source_address(record.get('source_ref'))
    return None


def shared_key_sections(marker, domain, address, columns):
    if not domain or not address or not columns or marker.get('binding') != digest(domain, address, columns):
        return set()
    # A pre-v2 receipt establishes field applicability only. Retain its
    # locator for audit without treating it as a declaration of a shared key.
    claimed = marker.get('shared_key_sections_v2')
    located = marker.get('key_sections_v1')
    if not isinstance(claimed, list) or not isinstance(located, list):
        return set()
    return {ref for ref in claimed if isinstance(ref, str) and re.fullmatch('[a-f0-9]{64}', ref) and ref in located}


def _shared_proofs(marker, domain, address, columns):
    sections = shared_key_sections(marker, domain, address, columns)
    interpreted_sections = {claim.get('section') for claim in marker.get('interpreted_key_claims_v1') or ()
                            if isinstance(claim, dict) and isinstance(claim.get('section'), str)}
    proofs = {('document', ref) for ref in sections - interpreted_sections}
    registries = set()
    for claim in marker.get('registry_key_claims_v1') or ():
        if not isinstance(claim, dict) or not isinstance(claim.get('section'), str) or claim['section'] not in sections:
            continue
        identity = [claim.get(field) for field in ('issuer', 'catalog', 'version')]
        roles = claim.get('key_roles')
        if (isinstance(roles, list) and len(roles) == len(columns)
                and all(isinstance(x, str) and x.strip() for x in (*identity, *roles))
                and len(set(roles)) == len(roles)):
            registries.add(('registry', digest('document_key_registry_v1', identity, roles)))
    if len(registries) > 1:
        return set()
    interpretations = set()
    for claim in marker.get('interpreted_key_claims_v1') or ():
        if (not isinstance(claim, dict) or claim.get('section') not in sections
                or claim.get('domain') != domain or claim.get('key_columns') != list(columns)
                or not isinstance(claim.get('quote'), str) or claim.get('quote_digest') != digest(claim['quote'])
                or not all(claim.get(field) for field in ('document_id', 'document_revision', 'section_id'))):
            continue
        roles = claim.get('key_roles')
        if (isinstance(roles, list) and len(roles) == len(columns) and len(set(roles)) == len(roles)
                and all(isinstance(role, str) and role and role in claim['quote'] for role in roles)):
            interpretations.add(('interpretation', digest('interpreted_identity_axis_v1', domain,
                                                         claim.get('version'), roles)))
    if len(interpretations) > 1:
        return set()
    return proofs | registries | interpretations


def cross_table_identity_issues(graph, draft, authorities, fusion):
    """An authored type alone cannot align two tables' independently observed instances."""
    declarations = {}
    issues = []
    for entity in draft.entities:
        rule = next((r for r in fusion.entity_rules if r.entity_type == entity.type
                     and set(entity.key_names) == set(r.key_mapping.values())), None)
        if rule and rule.source_bindings:
            from .fusion_bindings import draft_binding_issues
            issues.extend(draft_binding_issues(entity, rule))
        if len({(m.source_id, m.schema_name, m.table) for m in entity.mappings}) < 2:
            continue
        if rule and entity.identity_domain in (None, rule.identity_domain):
            continue
        declarations[entity.type] = entity
    if not declarations:
        return issues
    collisions = defaultdict(int)
    for node in graph.get('nodes', []):
        entity = declarations.get(node['type'])
        if entity is None or node.get('identity_origin', 'row') != 'row':
            continue
        locations, mapping_numbers = set(), set()
        common = None
        supports = node.get('supports') or []
        for support in supports:
            prov = support.get('provenance') or {}
            number = prov.get('mapping')
            address = source_address(prov.get('source'))
            locations.add(address)
            if isinstance(number, int):
                mapping_numbers.add(number)
            marker = authorities.get((entity.type, number), {})
            sections = _shared_proofs(marker, entity.identity_domain, address, prov.get('key_columns'))
            common = sections if common is None else common & sections
        if (len(locations) == 1 and None not in locations) or common:
            continue
        dictionary = _dictionary_binding(node, {'key_names': entity.key_names})
        if dictionary and dictionary[2] and len(dictionary[1]) <= 1:
            continue
        collisions[entity.type, tuple(sorted(mapping_numbers))] += 1
    return issues + [{'code': 'cross_table_identity_unverified', 'entity_type': name, 'mappings': list(numbers),
             'affected_nodes': count,
             'material_checks': [{'mapping': number, 'status': authorities.get((name, number), {}).get(
                                  'identity_material_status', 'material_missing_or_not_cited')}
                                 for number in numbers],
             'guidance': 'Use separate types or add the source scope to the key. To share identities, cite '
                         'the actual key definition. For ordinary business text, record an exact, source-bound '
                         'identity_explanation; read missing material or resolve contrary statements before retrying.'}
            for (name, numbers), count in sorted(collisions.items())]


def _global_authorities(node, definition, records, *, local_namespace):
    """A document can establish a common namespace. A catalog probe locates only its own observed row."""
    domain = definition.get('identity_domain')
    if not domain or node.get('identity_domain') != domain or not node.get('supports'):
        return set()
    common_docs = None
    bindings = set()
    complete = True
    for support in node['supports']:
        prov = support.get('provenance') or {}
        address = source_address(prov.get('source'))
        columns = prov.get('key_columns') or []
        marker = prov.get('global_identity_authority') or {}
        bound = address and columns and marker.get('binding') == digest(domain, address, columns)
        docs = _shared_proofs(marker, domain, address, columns)
        common_docs = docs if common_docs is None else common_docs & docs
        if not (address and local_namespace and not support.get('inherited_from')):
            complete = False
            continue
        # Read historical evidence only when that specific observation cites it.
        refs = set(support.get('evidence_refs') or ()) & set(definition.get('identity_evidence_refs') or ())
        witnessed = bound and marker.get('source_metadata') is True
        if not witnessed and not any(metadata_probe(records.get(ref) or {}) == address for ref in refs):
            complete = False
            continue
        evidence = _node_evidence({**node, 'supports': [support]}, definition, local_collision=True)
        if evidence:
            bindings.add(evidence)
        else:
            complete = False
    authorities = common_docs or set()
    if complete and bindings:
        authorities.add(('source', tuple(sorted(bindings))))
    return authorities


def _authority_groups(choices):
    """Each group shares one proof across every member, even when some have extra evidence."""
    groups = defaultdict(list)
    if all(len(proofs) == 1 for _, _, proofs in choices):
        for index, node, proofs in choices:
            groups[next(iter(proofs))].append((index, node))
        return groups
    by_proof = defaultdict(list)
    for pos, (_, _, proofs) in enumerate(choices):
        for proof in proofs:
            by_proof[proof].append(pos)
    # Prefer the proof shared by the most independent inputs. A record with
    # both documents A and B can never bridge A-only and B-only groups.
    assigned = set()
    for proof, positions in sorted(by_proof.items(), key=lambda entry: (
            -len({choices[pos][0] for pos in entry[1]}), entry[0][0] != 'document', digest(entry[0]))):
        for pos in positions:
            if pos not in assigned:
                groups[proof].append(choices[pos][:2])
                assigned.add(pos)
    return groups


def separate_unverified_ids(inputs, targets, *, excluded_types, explicit_domains=(), records=None, draft=False,
                           local_bases=(), source_namespaces=None):
    """Partition colliding IDs before accumulation. Each verified group must share one complete basis."""
    records = records or {}
    collisions = defaultdict(list)
    definitions = []
    for index, (owner, kind, graph) in enumerate(inputs):
        definitions.append({d['type']: d for d in graph.get('definitions', {}).get('entities', [])})
        for node in graph.get('nodes', []):
            if node.get('identity_origin', 'row') != 'row':
                continue
            # A previous split records its original ID. A later unit may
            # independently supply stronger evidence for just one side.
            previous = node.get('publication_identity_original_id', node['id'])
            nid = targets[index].get(node['id'], previous)
            collisions[nid, node['type'], node.get('identity_domain'), persisted_identity_key(node)].append((index, node))
    roles = {}
    candidates = []
    for (nid, _, _, _), entries in collisions.items():
        if len({index for index, _ in entries}) < 2:
            continue
        conflict = contradictory_qualifiers(node for _, node in entries)
        if not conflict:
            entries = [(index, node) for index, node in entries
                       if node.get('original_type', node['type']) not in excluded_types
                       and node['type'] not in excluded_types
                       and (node.get('original_type', node['type']), node.get('identity_domain')) not in explicit_domains
                       and (draft or definitions[index].get(node['type'], {}).get('identity_scope') == 'global')]
            if len({index for index, _ in entries}) < 2:
                continue
        # An old history may contain several locally disambiguated occurrences
        # with identical logical keys in one input. Never let their common
        # pre-split ID alone serve as evidence that those occurrences coincide.
        choices = []
        for index, node in entries:
            owner, kind, graph = inputs[index]
            declaration = definitions[index].get(node['type'], {})
            if conflict:
                proofs = {('qualifier_conflict', kind, owner, node['id'])}
            elif node['id'] in targets[index]:
                proofs = {('already_aligned', nid)}
            elif declaration.get('identity_scope') == 'global':
                proofs = _global_authorities(node, declaration, records, local_namespace=kind != 'base' or owner in local_bases)
            else:
                evidence = _node_evidence(node, declaration, local_collision=True)
                if index not in roles:
                    roles[index] = _relation_roles(graph)[1]
                proofs = {('source', evidence, tuple(sorted(roles[index][node['type']])))} if evidence else set()
            if not proofs:
                # A diamond DAG can contain the very same published occurrence twice.
                # Distinct raw drafts cannot acquire authority from their coincident ID.
                supports = node.get('supports') or []
                origins = {(o.get('contributor_id') or s.get('contributor_id'), o.get('validation_id'))
                           for s in supports for o in s.get('execution_origins', ())}
                if (kind == 'merged' and origins and all(all(o) for o in origins)
                        and all(s.get('execution_origins') for s in supports)):
                    proofs = {('publication', node['id'], tuple(sorted(origins)))}
                else:
                    proofs = {('local', kind, owner, node['id'])}
            if source_namespaces is not None:
                namespace = source_namespaces[index]
                boundary = ('declared', namespace) if namespace else ('unknown', kind, owner)
                proofs = {(*p, boundary) if p[0] in {'source', 'publication'} else p for p in proofs}
            choices.append((index, node, proofs))
        groups = _authority_groups(choices)
        if len(groups) == 1 and len({n['id'] for _, n in entries}) == 1:
            continue
        # Preserve a frozen lookup ID whenever possible. Stable owner order makes
        # the result independent of the order drafts were validated or submitted.
        ordered = sorted(groups.items(), key=lambda group: (
            all(inputs[i][1] not in {'base', 'merged'} for i, _ in group[1]),
            group[0][0] == 'local', min(inputs[i][0] for i, _ in group[1])))
        ids = []
        for number, (basis, members) in enumerate(ordered):
            prior = sorted({n['id'] for _, n in members if n.get('publication_identity_original_id') == nid})
            retain = number == 0 and (basis[0] == 'already_aligned'
                or any(inputs[i][1] in {'merged', 'base'} and n['id'] == nid for i, n in members))
            final = (prior[0] if prior else nid if retain else 'n_' + digest('publication_identity_v1', nid, basis))
            if final in ids:
                final = 'n_' + digest('publication_identity_v1', nid, basis)
            ids.append(final)
            for index, node in members:
                targets[index][node['id']] = final
                if final != nid:
                    node['publication_identity_original_id'] = nid
        if draft and len(ids) > 1:
            first = entries[0][1]
            candidates.append({'status': 'candidate_only', 'reason': ('conflicting_identity_qualifiers' if conflict else 'independent_drafts_unverified_identity'),
                'match_basis': 'same_typed_identity', 'entity_type': first['type'], 'identity': first['identity'],
                'contributors': sorted({inputs[i][0] for i, _ in entries}), 'node_ids': sorted(ids)})
    return candidates
