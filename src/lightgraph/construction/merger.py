"""Deterministic, permission-preserving multi-unit graph merger with sharded evidence persistence."""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from copy import deepcopy
import json
from pathlib import Path
from typing import Any
import unicodedata
import uuid

from lightgraph.contracts import canonical, digest
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import MergePolicyConfig
from lightgraph.source_values import identity_cell, persisted_identity_key
from .display import derive_display_graph
from .fusion import (deduplicate_supports, definition_conflict_diagnostics, edge_reference, label_priority, mark_relation_meanings,
                     merge_definition, prepare_graph, record_input_edge, relation_identity, retain_definition_variants)
from .context_topology import detect_context_cycles
from .hierarchy_replay import reconcile_hierarchy_projections
from .hierarchy_stage import original_stage_inputs, record_stage
from .models import COLUMN_IDENTITY_NAMES
from .observed_identity import align_dictionary_nodes, align_observed_nodes
from .source_namespaces import BINDING, isolate_physical_ids, namespace_groups, record_namespace
from .workspace import read_internal_yaml, write_yaml

SHARD_COUNT = 32


def _norm_label(value: Any) -> str:
    return ' '.join(unicodedata.normalize('NFKC', str(value or '')).casefold().split())


def _scope_ref_token(kind: str, contributor_id: str, ref: str) -> str:
    text = str(ref)
    if text.startswith(('base:', 'unit:')):
        return text
    return f'{kind}:{contributor_id}:{text}'


def _rewrite_evidence_in_graph(
    graph: dict[str, Any],
    *,
    kind: str,
    contributor_id: str,
    evidence_records: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Namespace every eN reference so independent units/projects never collide."""
    rewritten = deepcopy(graph)
    catalog: dict[str, dict[str, Any]] = {}

    def register(orig_ref: str) -> str:
        scoped = _scope_ref_token(kind, contributor_id, orig_ref)
        if scoped not in catalog:
            previous = (graph.get('evidence_catalog') or {}).get(orig_ref)
            if previous:
                # A nested DAG merge already carries the original source witness.
                catalog[scoped] = deepcopy(previous)
                return scoped
            record = (evidence_records or {}).get(orig_ref) or {}
            catalog[scoped] = {
                'scoped_ref': scoped,
                'contributor_id': contributor_id,
                'contributor_kind': kind,
                'original_ref': orig_ref,
                'kind': record.get('kind', 'unknown'),
                'source_ref': record.get('source_ref'),
                **({'details': {k: record['details'][k] for k in ('operation', 'complete') if k in record['details']}}
                   if record.get('kind') == 'source_probe' and record.get('details') else {}),
            }
        return scoped

    rewritten['evidence_refs'] = [register(r) for r in rewritten.get('evidence_refs', [])]
    for definition in rewritten.get('definitions', {}).get('entities', []):
        if definition.get('identity_evidence_refs'):
            definition['identity_evidence_refs'] = [register(r) for r in definition['identity_evidence_refs']]
    for item in rewritten.get('nodes', []) + rewritten.get('edges', []) + rewritten.get('unresolved_relations', []):
        for prov in item.get('provenance', []):
            if isinstance(prov, dict) and prov.get('evidence_refs'):
                prov['evidence_refs'] = [register(r) for r in prov['evidence_refs']]
        for support in item.get('supports', []):
            if not isinstance(support, dict):
                continue
            support.setdefault('contributor_id', contributor_id)
            for origin in support.get('execution_origins', []):
                origin.setdefault('contributor_id', contributor_id)
                if origin.get('evidence_refs'):
                    origin['evidence_refs'] = [register(r) for r in origin['evidence_refs']]
            prov = support.get('provenance')
            if isinstance(prov, dict) and prov.get('evidence_refs'):
                prov['evidence_refs'] = [register(r) for r in prov['evidence_refs']]
            if support.get('evidence_refs'):
                support['evidence_refs'] = [register(r) for r in support['evidence_refs']]
            endpoint = (prov or {}).get('metadata_endpoint') if isinstance(prov, dict) else None
            if isinstance(endpoint, dict) and isinstance(endpoint.get('column'), dict):
                refs = endpoint['column'].get('evidence_refs')
                if refs:
                    endpoint['column']['evidence_refs'] = [register(r) for r in refs]
    return rewritten, catalog


def _node_shared_allowed(
    node: dict[str, Any],
    *,
    kind: str,
    shared_types: set[str],
    base_node_ids: set[str],
) -> bool:
    if kind in {'base', 'merged'}:
        return True
    if node.get('id') in base_node_ids:
        # A local occurrence with the same code remains independent unless explicitly shared.
        return node.get('original_type', node.get('type')) in shared_types
    if node.get('identity_origin') == 'physical_column':
        # Physical columns have table-scoped identity [source_id, schema, table, column].
        return True
    return node.get('original_type', node.get('type')) in shared_types


def _local_catalog_targets(prepared, local_bases):
    """Resolve local catalog addresses after type qualification, preserving frozen lookup IDs."""
    targets = [{} for _ in prepared]
    pinned_ids = set()
    by_address = defaultdict(list)
    for index, (cid, kind, graph) in enumerate(prepared):
        for node in graph.get('nodes', []):
            if node.get('identity_origin') != 'physical_column':
                continue
            identity = node['identity']
            supports = node.get('supports', [])
            inherited = (kind == 'base' and cid not in local_bases) or any(
                s.get('inherited_from') for s in supports)
            if (inherited or set(identity) != set(COLUMN_IDENTITY_NAMES)
                    or not all(isinstance(v, str) and v for v in identity.values())
                    or not supports):
                pinned_ids.add(node['id'])
                continue
            address = (node['type'], *(identity[key] for key in COLUMN_IDENTITY_NAMES))
            by_address[address].append((index, kind, node['id']))
    for address, entries in by_address.items():
        if len(entries) == 1 and entries[0][2] not in pinned_ids:
            continue
        usable = [(kind not in {'base', 'merged'}, nid) for _, kind, nid in entries if nid not in pinned_ids]
        # Keep an existing local endpoint if possible. If a foreign publication
        # owns its ID, every local branch must choose the same replacement.
        target_id = min(usable)[1] if usable else 'n_' + digest('local_catalog_column_v1', address)
        for index, _, nid in entries:
            targets[index][nid] = target_id
    return targets


def _source_key_comparison(nodes, foreign_base_ids):
    """Compare recorded table addresses and ordered physical keys, not logical entity identity."""
    if any(node['id'] in foreign_base_ids for node in nodes):
        return 'unverified_source_namespace'
    tables, bindings = Counter(), Counter()
    incomplete_keys = False
    for node in nodes:
        supports = node.get('supports') or []
        if not supports:
            return 'incomplete_source_provenance'
        observed_tables, observed_bindings = set(), set()
        for support in supports:
            if support.get('inherited_from'):
                return 'unverified_source_namespace'
            provenance = support.get('provenance') or {}
            source = provenance.get('source') or {}
            table = tuple(source.get(field) for field in ('source_id', 'schema', 'table'))
            if not all(isinstance(value, str) and value for value in table):
                return 'incomplete_source_provenance'
            observed_tables.add(table)
            columns = provenance.get('key_columns')
            if (isinstance(columns, list) and columns and len(columns) == len(node['identity'])
                    and all(isinstance(c, str) and c for c in columns) and len(set(columns)) == len(columns)):
                observed_bindings.add((table, tuple(columns)))
            else:
                incomplete_keys = True
        # Repeated supports on a single node count as one witness for that node.
        tables.update(observed_tables)
        bindings.update(observed_bindings)
    common_to = max(tables.values())
    if common_to == 1:
        return 'disjoint_tables'
    if common_to < len(nodes):
        return 'partial_table_overlap'
    if len(nodes) in bindings.values():
        return 'shared_table_and_key_columns'
    if incomplete_keys:
        return 'incomplete_key_provenance'
    if max(bindings.values(), default=0) > 1:
        return 'partial_key_binding_overlap'
    return 'no_shared_key_binding'


def _same_name_candidates(nodes, seen_groups):
    """Explain cross-contributor homonyms without forming quadratic node pairs."""
    groups = defaultdict(set)
    for node in nodes.values():
        if node.get('identity_origin') == 'physical_column':
            continue
        for support in node.get('supports', []):
            name = _norm_label(support.get('observed_label') or support.get('label'))
            if name:
                groups[name].add((node['id'], support.get('contributor_id', '')))
    for name, members in sorted(groups.items()):
        node_ids = tuple(sorted({nid for nid, _ in members}))
        contributors = sorted({cid for _, cid in members if cid})
        if len(node_ids) < 2 or len(contributors) < 2 or node_ids in seen_groups:
            continue
        group = [nodes[nid] for nid in node_ids]
        types = sorted({node['type'] for node in group})
        if len(types) > 1:
            reason = 'same_label_different_entity_types'
        elif len({frozenset(node['identity']) for node in group}) > 1:
            reason = 'same_label_different_identity_keys'
        elif len({tuple(identity_cell(node, k)[0] for k in sorted(node['identity'])) for node in group}) > 1:
            reason = 'same_label_different_key_types'
        else:
            reason = 'same_label_different_identities'
        yield {'status': 'candidate_only', 'reason': reason, 'match_basis': 'normalized_label',
               'normalized_label': name, 'entity_types': types,
               'node_ids': list(node_ids), 'contributors': contributors}
        seen_groups.add(node_ids)


def _merge_contributor_nodes_and_edges(
    contributors: list[dict[str, Any]],
    *,
    policy: MergePolicyConfig,
    target_access_fingerprint: str | None = None,
    independent_outputs: bool = False,
    recognize_source_replays: bool = True,
    observe_before_hierarchy: Callable[[Iterable[dict]], None] | None = None,
    hierarchy_trace=None,
) -> dict[str, Any]:
    """Merge graphs while blocking unverified cross-unit auto-merges & auto-connections."""
    fusion = policy.fusion_policy
    shared_types = set(policy.shared_entity_types)
    for types in policy.shared_identity_domains.values():
        shared_types.update(types)

    explicit_domains = {(r.canonical_type, r.identity_domain) for r in fusion.entity_rules} if fusion else set()
    authority = set(policy.authority_sources)
    definitions = {'entities': {}, 'relations': {}}
    evidence_catalog: dict[str, dict[str, Any]] = {}
    evidence_refs: list[str] = []
    validation_snapshots: list[dict[str, Any]] = []

    base_node_ids: set[str] = set()
    base_remap: dict[str, str | None] = {}
    base_candidates: dict[str, set[str]] = defaultdict(set)
    prepared_contributors: list[tuple[str, str, dict[str, Any]]] = []
    key_versions = {c['graph']['source_key_semantics'] for c in contributors if c['graph'].get('source_key_semantics')}
    if len(key_versions) > 1:
        raise LightGraphError('published_graph_key_semantics_conflict')
    contributor_ids = set()
    upstream_isolations = []
    ordered = sorted(contributors, key=lambda c: (c.get('kind', 'unit'), c['contributor_id']))
    source_namespaces, source_groups = namespace_groups(ordered)
    for index, item in enumerate(ordered):
        cid = str(item['contributor_id'])
        ckind = str(item.get('kind', 'unit'))
        if (ckind, cid) in contributor_ids:
            raise LightGraphError('duplicate_merge_contributor')
        contributor_ids.add((ckind, cid))
        if (item['graph'].get('conflict_isolation') or {}).get('coverage') == 'incomplete':
            upstream_isolations.append({'kind': ckind, 'contributor_id': cid})
        rewritten, cat = _rewrite_evidence_in_graph(
            item['graph'],
            kind=ckind,
            contributor_id=cid,
            evidence_records=item.get('evidence_records'),
        )
        if fusion and fusion.entity_rules and not independent_outputs:
            local = (source_namespaces is None or len(source_groups) == 1 and source_namespaces[index] is not None)
            if ckind in {'base', 'merged'}:
                local = local and (item.get('local_source_namespace') is True
                                   or source_namespaces is not None and bool(rewritten.get(BINDING)))
            rewritten = prepare_graph(rewritten, fusion, copy=False, self_contained=ckind in {'base', 'merged'},
                                      local_namespace=local)
        evidence_catalog.update(cat)
        for ref in rewritten.get('evidence_refs', []):
            if ref not in evidence_refs:
                evidence_refs.append(ref)
        for snapshot in rewritten.get('validation_snapshots', []):
            origin = {'contributor_id': cid, 'kind': ckind, **snapshot}
            if origin not in validation_snapshots:
                validation_snapshots.append(origin)
        if rewritten.get('read_window'):
            validation_snapshots.append({
                'contributor_id': cid,
                'kind': ckind,
                'draft_id': rewritten.get('draft_id'),
                'draft_revision': rewritten.get('draft_revision'),
                'read_window': deepcopy(rewritten['read_window']),
            })
        prepared_contributors.append((cid, ckind, rewritten))

    observed_ids = [{} for _ in prepared_contributors]
    excluded_types = shared_types | ({rule.canonical_type for rule in fusion.entity_rules} if fusion else set())
    observation_witnesses = []
    source_replay_counts = Counter()
    local_bases = {str(item['contributor_id']) for item in contributors if item.get('local_source_namespace') is True}
    if independent_outputs:
        replay_status = 'independent_outputs'
    elif not recognize_source_replays:
        replay_status = 'disabled'
    elif len(prepared_contributors) < 2:
        replay_status = 'insufficient_contributors'
    else:
        replay_status = 'ran'
        for indices in source_groups:
            counts = {}
            remaps, witnesses = align_observed_nodes(
                [prepared_contributors[i] for i in indices], local_bases=local_bases, excluded_types=excluded_types,
                emit_candidates=policy.emit_alignment_candidates, diagnostics=counts)
            source_replay_counts.update(counts)
            for i, remap in zip(indices, remaps, strict=True):
                observed_ids[i] = remap
            if len(source_groups) == 1:
                observation_witnesses = witnesses
            else:
                observation_witnesses.extend({(indices[local], nid) for local, nid in group} for group in witnesses)

    variant_remaps = [{} for _ in prepared_contributors]
    if independent_outputs:
        _qualify_conflicting_output_types(prepared_contributors)
    else:
        variant_remaps = retain_definition_variants(
            [graph for _, _, graph in prepared_contributors],
            self_contained=[kind in {'base', 'merged'} for _, kind, _ in prepared_contributors],
            frozen_indices={i for i, (_, kind, _) in enumerate(prepared_contributors) if kind == 'base'})

    observed_ids = [{variants.get(original, original): target for original, target in observed.items()}
                    for observed, variants in zip(observed_ids, variant_remaps, strict=True)]
    node_targets = observed_ids
    dictionary_counts = Counter()
    dictionary_ids = [{} for _ in prepared_contributors]
    if replay_status == 'ran':
        node_targets = [dict(targets) for targets in observed_ids]
        for indices in source_groups:
            scoped = [prepared_contributors[i] for i in indices]
            catalog = _local_catalog_targets(scoped, local_bases)
            dictionaries, counts = align_dictionary_nodes(
                scoped, [observed_ids[i] for i in indices], local_bases=local_bases, excluded_types=excluded_types)
            dictionary_counts.update(counts)
            for i, dictionary, columns in zip(indices, dictionaries, catalog, strict=True):
                dictionary_ids[i] = dictionary
                node_targets[i].update(dictionary | columns)
    if len(source_groups) > 1 and not independent_outputs:
        node_targets = [dict(targets) for targets in node_targets]
        isolate_physical_ids(prepared_contributors, node_targets, observed_ids, dictionary_ids, source_groups,
                             source_namespaces, shared_types | {family for family, _ in explicit_domains})
    dictionary_targets = {target for mapping in dictionary_ids for target in mapping.values()}
    if len(prepared_contributors) > 1 and not independent_outputs:
        from .identity_authority import separate_unverified_ids
        node_targets = [dict(targets) for targets in node_targets]
        separate_unverified_ids(prepared_contributors, node_targets,
            excluded_types=shared_types, explicit_domains=explicit_domains,
            records=evidence_catalog, local_bases=local_bases, source_namespaces=source_namespaces)
    originals = {(str(item.get('kind', 'unit')), str(item['contributor_id'])): item['graph']
                 for item in contributors}
    for (cid, ckind, rewritten), targets in zip(prepared_contributors, node_targets, strict=True):
        if ckind == 'base':
            ids = {targets.get(n['id'], n['id']) for n in rewritten.get('nodes', [])}
            # Base lookups resolve by node id. Ambiguous projects cannot be used as a single endpoint.
            declared = {definition['type']: definition
                        for definition in rewritten.get('definitions', {}).get('entities', [])}
            explicit_shared = {n['id'] for n in rewritten.get('nodes', [])
                               if (n.get('original_type', n['type']), n.get('identity_domain')) in explicit_domains
                               or (declared.get(n['type'], {}).get('identity_scope') == 'global'
                                   and declared[n['type']].get('identity_domain') == n.get('identity_domain')
                                   and n.get('identity_domain') and declared[n['type']].get('identity_evidence_refs'))}
            if (ids & base_node_ids) - explicit_shared:
                raise LightGraphError('ambiguous_inherited_identity')
            base_node_ids.update(ids)
            for old, new in zip(originals[(ckind, cid)].get('nodes', []), rewritten.get('nodes', []), strict=True):
                new_id = targets.get(new['id'], new['id'])
                base_candidates[old['id']].add(new_id)
                if base_remap.setdefault(old['id'], new_id) != new_id:
                    # Both incompatible base variants remain visible. An actual
                    # lookup must still identify which contract it refers to.
                    base_remap[old['id']] = None

    merged_nodes: dict[str, dict[str, Any]] = {}
    merged_edges: dict[str, dict[str, Any]] = {}
    quarantined_nodes: set[str] = set()
    quarantined_edges: set[str] = set()
    conflict_details: list[dict[str, Any]] = []
    unresolved_relations: list[dict[str, Any]] = []
    context_cycle_diagnostics: list[dict[str, Any]] = []
    retained_labels = False
    retain_column_variants = (fusion.label_conflict_policy == 'retain_variants' if fusion
                              else recognize_source_replays and 'conflict_mode' not in policy.model_fields_set)

    # Index unshared/candidate nodes for alignment candidate generation (never auto-merged!).
    same_code_buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    column_label_buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)

    # Pass 1: collect definitions & all nodes from all contributors, retaining per-contributor id_remap.
    contributor_remaps: list[tuple[str, str, dict[str, Any], dict[str, str]]] = []
    frozen_presentation = {}
    for (cid, ckind, graph), variant_remap, targets in zip(prepared_contributors, variant_remaps, node_targets, strict=True):
        for category in ('entities', 'relations'):
            for defn in graph.get('definitions', {}).get(category, []):
                copied = deepcopy(defn)
                if category == 'entities':
                    copied.setdefault('display_mode', 'auto')
                    copied.setdefault('identity_origin', 'row')
                prev = definitions[category].get(copied['type'])
                merged = merge_definition(prev, copied)
                key = (category, copied['type'])
                if ckind == 'base':
                    # Bases are processed first. Keep the published presentation
                    # when a new table extends an otherwise compatible definition.
                    frozen_presentation[key] = {k: merged[k] for k in ('description', 'display_mode') if merged.get(k)}
                elif key in frozen_presentation:
                    merged.update(frozen_presentation[key])
                definitions[category][copied['type']] = merged

        id_remap: dict[str, str] = {}
        for raw_node in graph.get('nodes', []):
            node = deepcopy(raw_node)
            orig_id = node['id']
            can_share = (not independent_outputs or ckind == 'base') and _node_shared_allowed(
                node,
                kind=ckind,
                shared_types=shared_types,
                base_node_ids=base_node_ids,
            )
            if (node.get('original_type', node['type']), node.get('identity_domain')) in explicit_domains and not independent_outputs:
                can_share = True
            contract = definitions['entities'].get(node['type'], {})
            if (not independent_outputs and contract.get('identity_scope') == 'global'
                    and contract.get('identity_domain')
                    and contract.get('identity_domain') == node.get('identity_domain')
                    and contract.get('identity_evidence_refs')):
                can_share = True
            if orig_id in targets:
                node['id'] = targets[orig_id]
                id_remap[orig_id] = node['id']
            elif not can_share:
                # Isolate cross-unit entities without declared shared identity domain.
                scoped_id = 'n_' + digest('unit_scoped', cid, orig_id)
                node['id'] = scoped_id
                node['unit_id'] = cid
                node['unit_original_id'] = orig_id
                id_remap[orig_id] = scoped_id
            else:
                id_remap[orig_id] = orig_id

            target_id = node['id']
            existing = merged_nodes.get(target_id)
            if existing is None:
                merged_nodes[target_id] = node
            else:
                if (existing.get('type') != node.get('type') or existing.get('identity_domain') != node.get('identity_domain')
                        or persisted_identity_key(existing) != persisted_identity_key(node)
                        or existing.get('identity_origin', 'row') != node.get('identity_origin', 'row')):
                    raise LightGraphError('published_graph_identity_conflict')
                if existing.get('identity_origin') == 'physical_column':
                    from .metadata_columns import merge_column_supports
                    merge_column_supports(existing, node, retain_variants=retain_column_variants)
                    retained_labels |= retain_column_variants
                else:
                    for key in ('aliases', 'provenance', 'supports', 'english_names', 'descriptions'):
                        if key in node:
                            vals = existing.setdefault(key, [])
                            for v in node[key]:
                                if v not in vals:
                                    vals.append(deepcopy(v))
                    for key, values in node.get('properties', {}).items():
                        dest = existing.setdefault('properties', {}).setdefault(key, [])
                        for v in values:
                            if canonical(v) not in {canonical(x) for x in dest}:
                                dest.append(deepcopy(v))

            if node.get('identity_origin') == 'physical_column':
                norm = _norm_label(node.get('label') or node.get('identity', {}).get('column'))
                if norm:
                    column_label_buckets[norm].append({
                        'node_id': target_id,
                        'contributor_id': cid,
                        'entity_type': node.get('type'),
                        'identity': deepcopy(node.get('identity', {})),
                        'label': node.get('label'),
                    })
            else:
                same_code_buckets[(str(node.get('type')), persisted_identity_key(node))].append({
                    'node_id': target_id,
                    'contributor_id': cid,
                    'label': node.get('label'),
                    'identity': deepcopy(node.get('identity', {})),
                })

        for original, variant_id in variant_remap.items():
            id_remap[original] = id_remap[variant_id]
        contributor_remaps.append((cid, ckind, graph, id_remap))

    # Pass 2: process all edges across contributors after all nodes are indexed.
    for cid, ckind, graph, id_remap in contributor_remaps:
        for previous in graph.get('unresolved_relations', []):
            pending = deepcopy(previous)
            pending['candidate_endpoints'] = {key: sorted({id_remap.get(nid, nid) for nid in values})
                for key, values in pending.get('candidate_endpoints', {}).items()}
            pending['node_ids'] = sorted({id_remap.get(nid, nid) for nid in pending.get('node_ids', [])})
            if pending not in unresolved_relations:
                unresolved_relations.append(pending)
        # Preparation may have already qualified both endpoint and relation types.
        # Read the original identity from the contributor, before either rewrite.
        input_edges = originals[(ckind, cid)].get('edges', [])
        for original, raw_edge in zip(input_edges, graph.get('edges', []), strict=True):
            edge = deepcopy(raw_edge)
            ambiguous_endpoints = {}
            def resolve_endpoint(key):
                original = edge[key]
                if ckind in {'base', 'merged'}:
                    # A frozen publication is self-contained. Its historical lookup
                    # witnesses describe how the publisher established this edge.
                    if original not in id_remap:
                        raise LightGraphError('published_relation_endpoint_unavailable')
                    return id_remap[original]
                scopes = {any(match.get('endpoint') == key and match.get('inherited_node_id') == original
                              for match in support.get('lookup_matches', []))
                          for support in edge.get('supports', [])}
                if True in scopes:
                    if not graph.get('extends_base_graph'):
                        raise LightGraphError('published_relation_endpoint_unavailable')
                    canonical_id = base_remap.get(original, original)
                    if canonical_id is None:
                        ambiguous_endpoints[key] = sorted(base_candidates[original])
                        return None
                    if canonical_id not in base_node_ids:
                        raise LightGraphError('published_relation_endpoint_unavailable')
                    if False in scopes and id_remap.get(original, original) != canonical_id:
                        raise LightGraphError('ambiguous_inherited_identity')
                    for support in edge.get('supports', []):
                        for match in support.get('lookup_matches', []):
                            if match.get('endpoint') == key and match.get('inherited_node_id') == original:
                                match['inherited_node_id'] = canonical_id
                    return canonical_id
                if original in id_remap:
                    return id_remap[original]
                # Shared types can connect contributions that independently verify the same domain.
                node = merged_nodes.get(original)
                if node and node['type'] in shared_types:
                    return original
                raise LightGraphError('published_relation_endpoint_unavailable')
            src, dst = resolve_endpoint('source'), resolve_endpoint('target')
            if ambiguous_endpoints:
                candidates = {key: ambiguous_endpoints.get(key, [resolved])
                              for key, resolved in (('source', src), ('target', dst))}
                pending = deepcopy(edge)
                pending.update(id='unresolved_' + digest('definition_lookup_v1', cid, raw_edge['id'], candidates),
                               status='unresolved', reason='definition_endpoint_ambiguous',
                               original_endpoints={key: raw_edge[key] for key in ('source', 'target')},
                               candidate_endpoints=candidates,
                               node_ids=sorted({nid for values in candidates.values() for nid in values}))
                record_input_edge(pending, edge_reference(original), input_kind=ckind, contributor_id=cid)
                pending.pop('source', None)
                pending.pop('target', None)
                unresolved_relations.append(pending)
                continue
            edge['source'] = src
            edge['target'] = dst
            for support in edge.get('supports', []):
                for match in support.get('lookup_matches', []):
                    end = match.get('endpoint')
                    if end in ('source', 'target') and match.get('inherited_node_id') == raw_edge[end]:
                        match['inherited_node_id'] = edge[end]
            if independent_outputs and ckind != 'base':
                edge['id'] = 'r_' + digest('unit_scoped', cid, edge['id'])
            elif src != raw_edge['source'] or dst != raw_edge['target']:
                edge['id'] = relation_identity(edge)
            record_input_edge(edge, edge_reference(original), input_kind=ckind, contributor_id=cid)
            eid = edge['id']
            existing_edge = merged_edges.get(eid)
            if existing_edge is None:
                merged_edges[eid] = edge
            else:
                if any(existing_edge.get(k) != edge.get(k) for k in ('source', 'target', 'type')):
                    raise LightGraphError('published_graph_identity_conflict')
                for key in ('provenance', 'supports'):
                    if key in edge:
                        vals = existing_edge.setdefault(key, [])
                        for v in edge[key]:
                            if v not in vals:
                                vals.append(deepcopy(v))
                for key, values in edge.get('properties', {}).items():
                    dest = existing_edge.setdefault('properties', {}).setdefault(key, [])
                    for v in values:
                        if canonical(v) not in {canonical(x) for x in dest}:
                            dest.append(deepcopy(v))

    aligned_targets = {target for ids in observed_ids for target in ids.values()}
    # Label conflict detection & authority resolution on merged nodes.
    for node_id, node in list(merged_nodes.items()):
        if independent_outputs:
            break
        if node.get('identity_origin') == 'physical_column':
            continue
        contract = definitions['entities'].get(node['type'], {})
        default_global = (not fusion and 'conflict_mode' not in policy.model_fields_set
                          and contract.get('identity_scope') == 'global' and node.get('identity_domain')
                          and contract.get('identity_domain') == node['identity_domain']
                          and contract.get('identity_evidence_refs'))
        default_replay = (not fusion and 'conflict_mode' not in policy.model_fields_set
                          and (node_id in aligned_targets or node_id in dictionary_targets
                               or any(s.get('identity_basis') in {'observed_source_key_v1', 'observed_dictionary_key_v1',
                                                                 'observed_hierarchy_projection_v1'}
                                      for s in node.get('supports', []))))
        if fusion and fusion.label_conflict_policy == 'retain_variants' or default_global or default_replay:
            retained_labels = retained_labels or bool(default_global or default_replay)
            for support in node.get('supports', []):
                if support.get('observed_label') and not support.get('label'):
                    support['label'] = support['observed_label']
                if authority:
                    priority = label_priority(support, policy.authority_sources)
                    support.pop('label_priority', None)
                    if priority is not None:
                        support['label_priority'] = priority
            continue
        by_scope: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for support in node.get('supports', []):
            lbl = support.get('label')
            if not isinstance(lbl, str) or not lbl.strip():
                continue
            src_id = str((support.get('provenance') or {}).get('source', {}).get('source_id') or '')
            by_scope[str(support.get('scope_ref', 'public'))].append((lbl, src_id))
        for scope_ref, pairs in by_scope.items():
            distinct_labels = {lbl for lbl, _ in pairs}
            if len(distinct_labels) <= 1:
                continue
            # Check if normalized forms agree or authority source disambiguates.
            norm_forms = {_norm_label(lbl) for lbl in distinct_labels}
            auth_labels = sorted({lbl for lbl, sid in pairs if sid in authority})
            if len(norm_forms) == 1 or len(auth_labels) == 1:
                chosen = (auth_labels or sorted(distinct_labels))[0]
                # Keep variants on their own supports so a restricted view cannot expose another scope.
                for support in node['supports']:
                    variant = support.get('label')
                    if (str(support.get('scope_ref', 'public')) != scope_ref
                            or not variant or variant == chosen):
                        continue
                    support['observed_label'] = variant
                    # This support establishes only the observed variant. The authoritative
                    # support supplies the canonical display name if the viewer can see it.
                    support['label'] = None
                    aliases = support.setdefault('aliases', [])
                    if variant not in aliases:
                        aliases.append(variant)
                    if variant not in node.setdefault('aliases', []):
                        node['aliases'].append(variant)
                continue
            label_policy = fusion.label_conflict_policy if fusion else policy.conflict_mode
            if label_policy == 'strict':
                raise LightGraphError('published_graph_identity_conflict')
            quarantined_nodes.add(node_id)
            conflict_details.append({
                'code': 'same_identity_conflicting_label',
                'node_id': node_id,
                'scope_ref': scope_ref,
                'labels': sorted(distinct_labels),
            })

    if quarantined_nodes:
        for eid, edge in list(merged_edges.items()):
            if edge['source'] in quarantined_nodes or edge['target'] in quarantined_nodes:
                quarantined_edges.add(eid)
        for nid in quarantined_nodes:
            merged_nodes.pop(nid, None)
        for eid in quarantined_edges:
            merged_edges.pop(eid, None)

    # Global acyclic guard across merged contributors using iterative Kosaraju SCC.
    for rel_def in definitions['relations'].values():
        if independent_outputs:
            break
        if not rel_def.get('acyclic'):
            continue
        rel_type = rel_def['type']
        rel_edges = [e for e in merged_edges.values() if e['type'] == rel_type]
        topology = detect_context_cycles(rel_edges)
        cyclic_ids = set(topology['cycle_edge_ids'])
        if not topology['complete']:
            context_cycle_diagnostics.append({'status': 'unresolved',
                'reason': 'context_cycle_analysis_incomplete', 'relation': rel_type,
                'node_ids': sorted({edge[end] for edge in rel_edges for end in ('source', 'target')}),
                'edge_ids': sorted(edge['id'] for edge in rel_edges)})
        if cyclic_ids and policy.conflict_mode == 'strict':
            raise LightGraphError('published_relation_cycle')
        for edge in rel_edges:
            if edge['id'] in cyclic_ids:
                quarantined_edges.add(edge['id'])
                del merged_edges[edge['id']]
                conflict_details.append({'code': 'cross_unit_relation_cycle_isolated',
                                         'edge_id': edge['id'], 'relation': rel_type,
                                         'source': edge['source'], 'target': edge['target']})

    # Recompute top-level primary labels and sort deterministically.
    for node in merged_nodes.values():
        labels = sorted({s['label'] for s in node.get('supports', []) if s.get('label') is not None})
        if labels and not node.get('label'):
            node['label'] = labels[0]
        if 'aliases' in node:
            node['aliases'] = sorted(set(node['aliases']))
        if 'english_names' in node:
            node['english_names'] = sorted(set(node['english_names']))
        if 'descriptions' in node:
            node['descriptions'] = sorted(set(node['descriptions']))

    # Remaining same-name or same-code suggestions have no automatic identity authority.
    candidate_identity_merges: list[dict[str, Any]] = []
    candidate_column_alignments: list[dict[str, Any]] = []
    if policy.emit_alignment_candidates:
        foreign_base_ids = {target for cid, kind, _, remap in contributor_remaps
                            if kind == 'base' and cid not in local_bases for target in remap.values()}
        node_source_groups = defaultdict(set)
        if len(source_groups) > 1:
            for group, indices in enumerate(source_groups):
                for index in indices:
                    for target in contributor_remaps[index][3].values():
                        node_source_groups[target].add(group)
        for (etype, _), items in sorted(same_code_buckets.items()):
            retained_items = [item for item in items if item['node_id'] in merged_nodes]
            distinct_nodes = sorted({item['node_id'] for item in retained_items})
            if len(distinct_nodes) > 1:
                candidate_identity_merges.append({
                    'status': 'candidate_only',
                    'reason': 'cross_unit_unverified_identity_domain',
                    'match_basis': 'same_typed_identity',
                    'source_key_comparison': ('unverified_source_namespace'
                        if len({g for nid in distinct_nodes for g in node_source_groups.get(nid, ())}) > 1
                        else _source_key_comparison([merged_nodes[nid] for nid in distinct_nodes], foreign_base_ids)),
                    'entity_type': etype,
                    'node_ids': distinct_nodes,
                    'contributors': sorted({item['contributor_id'] for item in retained_items}),
                    'identity': retained_items[0]['identity'],
                })
        seen_groups = {tuple(candidate['node_ids']) for candidate in candidate_identity_merges}
        physical_candidates = {}
        for witnesses in observation_witnesses:
            ids = tuple(sorted({contributor_remaps[index][3][nid] for index, nid in witnesses}))
            if len(ids) < 2 or ids in seen_groups or not all(nid in merged_nodes for nid in ids):
                continue
            physical_candidates[ids] = {
                'status': 'candidate_only', 'reason': 'source_key_requires_role_review',
                'match_basis': 'same_source_observation', 'node_ids': list(ids),
                'contributors': sorted({contributor_remaps[index][0] for index, _ in witnesses}),
                'entity_types': sorted({merged_nodes[nid]['type'] for nid in ids})}
            seen_groups.add(ids)
        candidate_identity_merges.extend(physical_candidates[ids] for ids in sorted(physical_candidates))
        for _, kind, upstream, remap in contributor_remaps:
            if kind not in ('unit', 'merged'):
                continue
            for prior in upstream.get('candidate_identity_merges', []):
                original = prior.get('node_ids') or []
                if (prior.get('status') != 'candidate_only' or not original
                        or any(remap.get(n) not in merged_nodes for n in original)):
                    continue
                ids = tuple(sorted({remap[n] for n in original}))
                if len(ids) < 2 or ids in seen_groups:
                    continue
                retained = {**deepcopy(prior), 'node_ids': list(ids)}
                retained.pop('candidate_id', None)
                if 'entity_types' in retained:
                    retained['entity_types'] = sorted({merged_nodes[n]['type'] for n in ids})
                candidate_identity_merges.append(retained)
                seen_groups.add(ids)
        candidate_identity_merges.extend(_same_name_candidates(merged_nodes, seen_groups))
        for norm, items in sorted(column_label_buckets.items()):
            unique_cols = {i['node_id']: i for i in items}
            if len(unique_cols) > 1:
                candidate_column_alignments.append({
                    'status': 'candidate_only',
                    'reason': 'same_label_requires_caliber_review',
                    'normalized_label': norm,
                    'columns': sorted(unique_cols.values(), key=lambda x: x['node_id']),
                })

    result: dict[str, Any] = {
        'format_version': 1,
        'target': 'graph',
        'status': 'published',
        'support_visibility_version': 1,
        'semantic_origin': 'agent_authored',
        'definitions': {k: [v[name] for name in sorted(v)] for k, v in definitions.items()},
        'nodes': sorted(merged_nodes.values(), key=lambda n: n['id']),
        'edges': sorted(merged_edges.values(), key=lambda e: e['id']),
        'evidence_refs': evidence_refs,
        'evidence_catalog': evidence_catalog,
        'validation_snapshots': validation_snapshots,
        'candidate_identity_merges': candidate_identity_merges,
        'candidate_column_alignments': candidate_column_alignments,
        'unresolved_relations': sorted(unresolved_relations, key=lambda item: item['id']),
        'context_cycle_diagnostics': context_cycle_diagnostics,
        'conflict_isolation': {
            'policy': 'merge_quarantine_v1',
            'node_ids': sorted(quarantined_nodes),
            'edge_ids': sorted(quarantined_edges),
            'nodes': len(quarantined_nodes),
            'edges': len(quarantined_edges),
            'details': conflict_details,
            'contributors_with_gaps': upstream_isolations,
            'coverage': 'incomplete' if (quarantined_nodes or quarantined_edges or upstream_isolations) else 'complete',
        },
    }
    # A physical table address is local to one project. Unverified intermediate
    # publications retain their decisions; verified stages have already supplied their leaves.
    hierarchy_collapsed = None
    hierarchy_diagnostics = {}
    review_stats = None
    compatible_namespace = source_namespaces is None or len(source_groups) == 1 and source_namespaces[0] is not None
    if hierarchy_trace is not None:
        hierarchy_trace.start(result)
    if replay_status in ('ran', 'insufficient_contributors') and prepared_contributors and compatible_namespace and all(
        kind == 'unit' and not graph.get('extends_base_graph') for _, kind, graph in prepared_contributors
    ):
        if observe_before_hierarchy is not None:
            observe_before_hierarchy([edge_reference(e) for e in result['edges']])
        reviewed_pairs = None
        if policy.reviewed_key_pairs:
            from .reviewed_keys import reviewed_key_groups
            reviewed_pairs, review_stats = reviewed_key_groups(result, contributors, policy.reviewed_key_pairs, contributor_remaps)
        hierarchy_collapsed = reconcile_hierarchy_projections(
            result, excluded_types=excluded_types, diagnostics=hierarchy_diagnostics, trace=hierarchy_trace,
            emit_candidates=policy.emit_alignment_candidates, reviewed_pairs=reviewed_pairs, review_stats=review_stats,
            deduplicate_changed=not (fusion or retained_labels))
    result['definition_conflicts'] = definition_conflict_diagnostics(result)
    if fusion:
        result['fusion_policy'] = fusion.model_dump(mode='json')
        result['fusion_policy_digest'] = digest(result['fusion_policy'])
        result['label_conflict_policy'] = fusion.label_conflict_policy
    elif retained_labels:
        result['label_conflict_policy'] = 'retain_variants'
    if fusion or retained_labels:
        for item in result['nodes'] + result['edges']:
            if 'supports' in item:
                deduplicate_supports(item)
    if independent_outputs and any(c['graph'].get('label_conflict_policy') == 'retain_variants' for c in contributors):
        result['label_conflict_policy'] = 'retain_variants'
    if key_versions:
        result['source_key_semantics'] = next(iter(key_versions))
    if target_access_fingerprint:
        result['access_fingerprint'] = target_access_fingerprint
    # These counts belong to this exact merge pass, including hidden inputs.
    # filter_graph_scopes drops fusion_diagnostics from partial views.
    counts = merge_accounting(contributors, result)
    replay_inputs = sum(len(ids) for ids in observed_ids)
    counts.update({
        'source_replay_status': replay_status,
        'explicit_entity_rules': len(fusion.entity_rules) if fusion else 0,
        'explicit_rule_preparation_ran': bool(fusion and fusion.entity_rules and contributors and not independent_outputs),
        'source_replay_matched_inputs': replay_inputs,
        'source_replay_groups': len(aligned_targets),
        'source_replay_collapsed_nodes': replay_inputs - len(aligned_targets),
        **dictionary_counts,
        'collapsed_nodes_before_quarantine': counts['input_nodes'] - counts['output_nodes'] - len(quarantined_nodes),
        'input_physical_column_nodes': sum(n.get('identity_origin') == 'physical_column'
            for c in contributors for n in c['graph'].get('nodes', ())),
        'output_physical_column_nodes': sum(n.get('identity_origin') == 'physical_column' for n in merged_nodes.values()),
        **{f'source_replay_{name}': value for name, value in source_replay_counts.items()},
    })
    if hierarchy_collapsed is not None:
        counts['hierarchy_replay_collapsed_nodes'] = hierarchy_collapsed
    if hierarchy_diagnostics.get('global_node_inputs'):
        counts['hierarchy_replay_diagnostics'] = hierarchy_diagnostics
    if review_stats is not None:
        counts['reviewed_key_pairs'] = dict(review_stats)
    result['fusion_diagnostics'] = counts
    derive_display_graph(result, copy=False)
    record_namespace(result, source_namespaces, source_groups)
    record_stage(result, contributors, policy)
    return result


def _qualify_conflicting_output_types(prepared):
    """Keep both branch definitions when displaying independent graphs in one canvas.

    Only the projection gets qualified names. Source publications retain the
    original names, IDs and evidence. Relations follow their branch's entities.
    """
    mark_relation_meanings(graph for _, _, graph in prepared)
    for category, items in (('entities', 'nodes'), ('relations', 'edges')):
        shapes = defaultdict(set)
        for _, _, graph in prepared:
            for definition in graph.get('definitions', {}).get(category, []):
                comparable = {k: v for k, v in definition.items() if k != 'description'}
                if category == 'entities':
                    comparable.setdefault('display_mode', 'auto')
                    comparable.setdefault('identity_origin', 'row')
                shapes[definition['type']].add(canonical(comparable))
        occupied = set(shapes)
        for cid, kind, graph in prepared:
            if kind == 'base':
                continue
            aliases = {}
            for definition in graph.get('definitions', {}).get(category, []):
                name = definition['type']
                if len(shapes[name]) < 2:
                    continue
                label = f'{name[:98 - len(cid)]} ({cid})'[:100]
                while label in occupied:
                    label = f'{name[:66]}_{digest(label, cid)[:32]}'
                occupied.add(label)
                aliases[name] = label
                definition['type'] = label
            for item in graph.get(items, []):
                if item['type'] in aliases:
                    item['type'] = aliases[item['type']]
            if category == 'entities':
                for relation in graph.get('definitions', {}).get('relations', []):
                    for endpoint in ('source_type', 'target_type'):
                        if relation.get(endpoint) in aliases:
                            relation[endpoint] = aliases[relation[endpoint]]


def identity_candidate_node_counts(graph):
    """Count distinct nodes across candidate groups, including nodes listed in several groups."""
    memberships = Counter()
    for group in graph.get('candidate_identity_merges', ()):
        ids = group.get('node_ids') if isinstance(group, dict) else None
        if not isinstance(ids, list) or not ids or not all(isinstance(node_id, str) for node_id in ids):
            # A historical report without group membership cannot supply these counts.
            return {}
        memberships.update(set(ids))
    return {
        'candidate_identity_nodes': len(memberships),
        'candidate_identity_nodes_in_multiple_groups': sum(count > 1 for count in memberships.values()),
    }


def merge_accounting(contributors, graph):
    """Counts for this merge attempt, including inherited inputs; candidates may overlap."""
    if 'fusion_diagnostics' in graph:
        return dict(graph['fusion_diagnostics'])
    isolation = graph.get('conflict_isolation') or {}
    pair_counts = Counter()
    self_loops = 0
    for edge in graph['edges']:
        source, target = edge['source'], edge['target']
        pair_counts[source, target] += 1
        if source == target:
            self_loops += 1
    return {
        'input_nodes': sum(len(c['graph'].get('nodes', ())) for c in contributors),
        'input_edges': sum(len(c['graph'].get('edges', ())) for c in contributors),
        'output_nodes': len(graph['nodes']),
        'output_edges': len(graph['edges']),
        'output_directed_endpoint_pairs': len(pair_counts),
        'output_multi_edge_pairs': sum(size > 1 for size in pair_counts.values()),
        'output_endpoint_pair_excess': len(graph['edges']) - len(pair_counts),
        'output_self_loop_edges': self_loops,
        'candidate_identity_groups': len(graph.get('candidate_identity_merges', ())),
        **identity_candidate_node_counts(graph),
        'quarantined_nodes': isolation.get('nodes', 0),
        'quarantined_edges': isolation.get('edges', 0),
        'unresolved_relations': len(graph.get('unresolved_relations', ())),
    }


def merge_construction_graphs(
    contributors: list[dict[str, Any]],
    *,
    target_access_fingerprint: str | None = None,
    merge_policy: MergePolicyConfig | dict[str, Any] | None = None,
    independent_outputs: bool = False,
    recognize_source_replays: bool = True,
    observe_before_hierarchy: Callable[[Iterable[dict]], None] | None = None,
    hierarchy_trace=None,
) -> dict[str, Any]:
    """Merge authorized graph contributors.

    Callers may bind each contributor to the same project with `source_namespace`.
    A complete deterministic hierarchy stage can then recover its verified original
    observations, or accept exact `original_units` on the merged contributor.
    """
    policy = (
        merge_policy if isinstance(merge_policy, MergePolicyConfig)
        else MergePolicyConfig.model_validate(merge_policy or {})
    )
    if not independent_outputs and recognize_source_replays:
        contributors = original_stage_inputs(contributors, policy, target_access_fingerprint)
    return _merge_contributor_nodes_and_edges(
        contributors,
        policy=policy,
        target_access_fingerprint=target_access_fingerprint,
        independent_outputs=independent_outputs,
        recognize_source_replays=recognize_source_replays,
        observe_before_hierarchy=observe_before_hierarchy,
        hierarchy_trace=hierarchy_trace,
    )


def _shard_bucket(identifier: str) -> str:
    return f'shard_{int(digest(identifier)[:4], 16) % SHARD_COUNT:02x}.json'


def write_sharded_graph(construction_dir: str | Path, merged: dict[str, Any], *, context_fingerprint: str,
                        unit_attempts: dict[str, str] | None = None, result_policy_id: str | None = None) -> dict[str, Any]:
    """Publish an immutable Parquet snapshot (name retained for API compatibility)."""
    from .graph_storage import write_graph_record
    from .paths import io_path
    root = io_path(construction_dir)
    (root / 'published').mkdir(parents=True, exist_ok=True)
    publication = 'published/' + uuid.uuid4().hex + '.graph'
    graph = {**merged, 'storage_layout': 'parquet_v1'}
    write_graph_record(root / publication, graph)
    manifest_path = root / 'manifest.yaml'
    manifest = (
        read_internal_yaml(manifest_path)
        if manifest_path.exists()
        else {'format_version': 1, 'target': 'graph', 'drafts': {}, 'evidence': {}, 'evidence_history': {}, 'publications': {}}
    )
    old_head = manifest.get('current_graph')
    if old_head and 'version_id' not in old_head:
        manifest.setdefault('graph_history', []).append(deepcopy(old_head))
    manifest['current_graph'] = {
        'context_fingerprint': context_fingerprint,
        **({'result_policy_id': result_policy_id} if result_policy_id else {}),
        **({'workflow_units': unit_attempts} if unit_attempts is not None else {}),
        'access_fingerprint': merged.get('access_fingerprint'),
        'path': publication,
        'storage_layout': 'parquet_v1',
    }
    from .snapshot_registry import record_graph_head
    record_graph_head(manifest, manifest['current_graph'], root=root, graph_kind='project_full',
                      coverage=merged.get('conflict_isolation', {}).get('coverage', 'complete'))
    write_yaml(manifest_path, manifest)
    return {
        'core_path': publication,
        'shard_count': 2,
        'storage_layout': 'parquet_v1',
        'nodes': len(merged.get('nodes', [])),
        'edges': len(merged.get('edges', [])),
    }


def load_sharded_graph(construction_dir: str | Path, core: dict[str, Any]) -> dict[str, Any]:
    """Rehydrate full node/edge supports transparently from sharded storage."""
    if core.get('storage_layout') != 'sharded_v1':
        return core
    root = Path(construction_dir)
    loaded_shards: dict[str, dict[str, Any]] = {}
    for bucket, rel in (core.get('evidence_shards') or {}).items():
        path = (root / rel).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            raise LightGraphError('graph_workspace_record_unavailable')
        loaded_shards[bucket] = json.loads(path.read_text(encoding='utf-8'))

    hydrated = deepcopy(core)
    for category in ('nodes', 'edges'):
        for item in hydrated.get(category, []):
            extra = loaded_shards.get(_shard_bucket(item['id']), {}).get(category, {}).get(item['id'])
            if not isinstance(extra, dict) or 'supports' not in extra or 'provenance' not in extra:
                raise LightGraphError('graph_workspace_record_unavailable')
            item.update(supports=extra['supports'], provenance=extra['provenance'])
    return hydrated
