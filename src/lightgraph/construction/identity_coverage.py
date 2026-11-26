"""Read-only census of identity evidence retained in committed extract checkpoints."""
from collections import Counter, defaultdict
from itertools import combinations
import re

from lightgraph.contracts import digest
from .fusion import definition_contract
from .identity_authority import _global_authorities, _shared_proofs, source_address


def _document_inventory(item):
    records = item.get('evidence_records') or {}
    versions, read_sections, read_docs, docs = set(), set(), set(), set()
    for row in records.values():
        if row.get('kind') not in {'document_version', 'document_section'}:
            continue
        detail = row.get('details') or {}
        doc, rev = detail.get('document_id'), detail.get('revision')
        if not doc:
            continue
        docs.add(doc)
        if rev is not None:
            versions.add((doc, rev))
        if row['kind'] == 'document_section' and detail.get('complete') is True and detail.get('section_id'):
            read_docs.add(doc)
            read_sections.add((doc, rev, detail['section_id']))
    archived = item.get('has_evidence')
    state = ('present' if records or archived is True else
             'not_archived' if archived is False else 'unknown')
    return {
        'evidence_archive': state,
        'full_visible_inventory': 'not_archived',
        'observed_visible_lower_bound': len(docs),
        'document_ids': sorted(docs),
        'document_version_count': len(versions),
        'complete_section_count': len(read_sections),
        'documents_with_complete_section_read': sorted(read_docs),
    }


def _definitions(graph):
    type_counts = Counter(n['type'] for n in graph.get('nodes', ()))
    for definition in graph.get('definitions', {}).get('entities', ()):
        kind = definition['type']
        contract = definition_contract(definition)
        yield definition, {
            'type': kind,
            'family': contract['type'],
            'contract_digest': digest(contract),
            'archived_variant_digest': (definition.get('definition_variant') or {}).get('contract_digest'),
            'node_count': type_counts[kind],
        }


def _unit(item):
    graph = item['graph']
    records = item.get('evidence_records') or {}
    catalog = graph.get('evidence_catalog') or {}
    documents = _document_inventory(item)
    definitions, scopes, ref_kinds = {}, Counter(), Counter()
    rows = []
    for definition, row in _definitions(graph):
        definitions[definition['type']] = definition
        refs = definition.get('identity_evidence_refs') or []
        kinds = Counter((records.get(r) or catalog.get(r) or {}).get('kind', 'unresolved') for r in refs)
        scope = definition.get('identity_scope') or 'unknown'
        domain = definition.get('identity_domain')
        rows.append({**row, 'identity_scope': scope, 'identity_domain_digest': digest(domain) if domain else None,
                     'identity_evidence_refs': len(refs), 'identity_evidence_ref_kinds': dict(sorted(kinds.items()))})
        scopes[scope] += 1
        ref_kinds.update(kinds)
    nodes = graph.get('nodes', ())
    counts = dict.fromkeys(('supports', 'supports_with_authority_marker', 'nodes_with_authority_marker',
                            'supports_with_key_sections_v1', 'nodes_with_key_sections_v1',
                            'supports_claiming_shared_sections_v2', 'supports_claiming_registry',
                            'supports_with_bound_shared_sections',
                            'supports_with_bound_registry', 'nodes_with_shared_section_proof',
                            'nodes_with_registry_proof'), 0)
    located = set()
    node_proofs = defaultdict(set)
    for node in nodes:
        definition = definitions.get(node['type'], {})
        domain = definition.get('identity_domain')
        has_marker = has_locator = has_bound_proof = False
        for support in node.get('supports') or ():
            counts['supports'] += 1
            prov = support.get('provenance') or {}
            marker = prov.get('global_identity_authority') or {}
            if marker:
                counts['supports_with_authority_marker'] += 1
                has_marker = True
            counts['supports_claiming_shared_sections_v2'] += bool(marker.get('shared_key_sections_v2'))
            counts['supports_claiming_registry'] += bool(marker.get('registry_key_claims_v1'))
            sections = marker.get('key_sections_v1')
            if isinstance(sections, list) and sections:
                has_locator = True
                counts['supports_with_key_sections_v1'] += 1
                located.update(s for s in sections if isinstance(s, str) and re.fullmatch('[a-f0-9]{64}', s))
            if marker.get('shared_key_sections_v2') and domain and domain == node.get('identity_domain'):
                proofs = _shared_proofs(marker, domain, source_address(prov.get('source')), prov.get('key_columns'))
                kinds = {kind for kind, _ in proofs}
                has_bound_proof |= bool(proofs)
                counts['supports_with_bound_shared_sections'] += 'document' in kinds
                counts['supports_with_bound_registry'] += 'registry' in kinds
        counts['nodes_with_authority_marker'] += has_marker
        counts['nodes_with_key_sections_v1'] += has_locator
        if has_bound_proof and definition.get('identity_scope') == 'global':
            proofs = _global_authorities(node, definition, {}, local_namespace=False)
            kinds = {kind for kind, _ in proofs}
            counts['nodes_with_shared_section_proof'] += 'document' in kinds
            counts['nodes_with_registry_proof'] += 'registry' in kinds
            for kind, token in proofs:
                node_proofs[kind].add(token)
    result = {'artifact_id': item.get('artifact_id'), 'documents': documents, 'nodes': len(nodes),
              'definitions_with_identity_domain': sum(bool(row['identity_domain_digest']) for row in rows),
              'definitions_by_scope': dict(sorted(scopes.items())), 'identity_evidence_ref_kinds': dict(sorted(ref_kinds.items())),
              'definitions': sorted(rows, key=lambda row: row['type']), 'distinct_key_sections_v1': len(located),
              'distinct_bound_shared_sections_on_nodes': len(node_proofs['document']),
              'distinct_bound_registries_on_nodes': len(node_proofs['registry']), **counts}
    return result, node_proofs


def identity_evidence_coverage(inputs, committed_graph):
    """Count archived documents, locally usable proofs and actual committed variants without reading source rows."""
    units, proofs, access = {}, {}, {}
    families = defaultdict(list)
    for item in sorted(inputs, key=lambda item: item['contributor_id']):
        owner = item['contributor_id']
        if owner in units:
            raise ValueError('duplicate_contributor_id')
        units[owner], proofs[owner] = _unit(item)
        access[owner] = item['graph'].get('access_fingerprint')
        for row in units[owner]['definitions']:
            families[row['family']].append({'contributor_id': owner, 'type': row['type'],
                'contract_digest': row['contract_digest'], 'archived_variant_digest': row['archived_variant_digest'],
                'node_count': row['node_count']})
    pairs = []
    for left, right in combinations(units, 2):
        left_docs, right_docs = (units[owner]['documents'] for owner in (left, right))
        doc_ids = sorted(set(left_docs['document_ids']) & set(right_docs['document_ids']))
        shared = proofs[left]['document'] & proofs[right]['document']
        registries = proofs[left]['registry'] & proofs[right]['registry']
        if not access[left] or not access[right]:
            fingerprint_status = 'missing'
        else:
            fingerprint_status = 'matched' if access[left] == access[right] else 'different'
        pairs.append({'contributors': [left, right], 'observed_document_ids_in_common': doc_ids,
                      'observed_document_overlap_status': 'observed' if doc_ids else (
                          'no_overlap_in_archived_records' if all(d['evidence_archive'] == 'present'
                              for d in (left_docs, right_docs)) else 'unknown'),
                      'intersecting_node_bound_shared_sections': sorted(shared),
                      'intersecting_node_bound_registry_digests': sorted(registries),
                      'cross_table_access_fingerprint_status': fingerprint_status,
                      'archived_cross_table_proof_route': 'shared_proof_present' if shared or registries
                                                         else 'no_shared_proof_observed'})
    committed = defaultdict(list)
    for _, row in _definitions(committed_graph):
        committed[row['family']].append(row)
    variants = []
    for family in sorted(families.keys() | committed.keys()):
        inputs_for_family = families[family]
        outputs = sorted(committed[family], key=lambda row: row['type'])
        combined = inputs_for_family + outputs
        if len({(row['type'], row['contract_digest']) for row in combined}) < 2:
            continue
        for row in inputs_for_family:
            row['matching_committed_types'] = [o['type'] for o in outputs if o['contract_digest'] == row['contract_digest']]
        variants.append({'family': family, 'input_contracts': inputs_for_family, 'committed_definitions': outputs,
                         'committed_node_count': sum(row['node_count'] for row in outputs)})
    candidates = committed_graph.get('candidate_identity_merges')
    return {'version': 1, 'units': units, 'unit_pairs': pairs, 'variant_families': variants,
            'cross_table_access_requirement': 'both_archived_fingerprints_present_and_equal',
            'same_physical_binding_grant_exception': 'not_evaluated',
            'entity_pair_outcomes': 'not_evaluated',
            'committed': {'node_count': len(committed_graph.get('nodes', ())),
                          'candidate_groups': len(candidates) if isinstance(candidates, list) else None}}
