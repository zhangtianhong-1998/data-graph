"""Deterministic, permission-preserving multi-unit graph merger with sharded evidence persistence."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import json
from pathlib import Path
from typing import Any
import unicodedata

from lightgraph.contracts import canonical, digest
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import MergePolicyConfig
from lightgraph.runtime.retention import atomic_text
from lightgraph.source_values import persisted_identity_key
from .display import derive_display_graph
from .statistics import directed_topology
from .workspace import safe_load_yaml, write_yaml

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
            record = (evidence_records or {}).get(orig_ref) or {}
            catalog[scoped] = {
                'scoped_ref': scoped,
                'contributor_id': contributor_id,
                'contributor_kind': kind,
                'original_ref': orig_ref,
                'kind': record.get('kind', 'unknown'),
                'source_ref': record.get('source_ref'),
            }
        return scoped

    rewritten['evidence_refs'] = [register(r) for r in rewritten.get('evidence_refs', [])]
    for item in rewritten.get('nodes', []) + rewritten.get('edges', []):
        for prov in item.get('provenance', []):
            if isinstance(prov, dict) and prov.get('evidence_refs'):
                prov['evidence_refs'] = [register(r) for r in prov['evidence_refs']]
        for support in item.get('supports', []):
            if not isinstance(support, dict):
                continue
            support.setdefault('contributor_id', contributor_id)
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
    if kind == 'base':
        return True
    if node.get('id') in base_node_ids:
        # A local occurrence with the same code remains independent unless explicitly shared.
        return node.get('type') in shared_types
    if node.get('identity_origin') == 'physical_column':
        # Physical columns have table-scoped identity [source_id, schema, table, column].
        return True
    return node.get('type') in shared_types


def _merge_contributor_nodes_and_edges(
    contributors: list[dict[str, Any]],
    *,
    policy: MergePolicyConfig,
    target_access_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Merge graphs while blocking unverified cross-unit auto-merges & auto-connections."""
    shared_types = set(policy.shared_entity_types)
    for types in policy.shared_identity_domains.values():
        shared_types.update(types)

    authority = set(policy.authority_sources)
    definitions = {'entities': {}, 'relations': {}}
    evidence_catalog: dict[str, dict[str, Any]] = {}
    evidence_refs: list[str] = []
    validation_snapshots: list[dict[str, Any]] = []

    base_node_ids: set[str] = set()
    prepared_contributors: list[tuple[str, str, dict[str, Any]]] = []
    key_versions = {c['graph']['source_key_semantics'] for c in contributors if c['graph'].get('source_key_semantics')}
    if len(key_versions) > 1:
        raise LightGraphError('published_graph_key_semantics_conflict')
    contributor_ids = set()
    upstream_isolations = []
    for item in sorted(contributors, key=lambda c: (c.get('kind', 'unit'), c['contributor_id'])):
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
        evidence_catalog.update(cat)
        for ref in rewritten.get('evidence_refs', []):
            if ref not in evidence_refs:
                evidence_refs.append(ref)
        if rewritten.get('read_window'):
            validation_snapshots.append({
                'contributor_id': cid,
                'kind': ckind,
                'draft_id': rewritten.get('draft_id'),
                'draft_revision': rewritten.get('draft_revision'),
                'read_window': deepcopy(rewritten['read_window']),
            })
        if ckind == 'base':
            ids = {n['id'] for n in rewritten.get('nodes', [])}
            # Base lookups resolve by node id. Ambiguous projects cannot be used as a single endpoint.
            if ids & base_node_ids:
                raise LightGraphError('ambiguous_inherited_identity')
            base_node_ids.update(ids)
        prepared_contributors.append((cid, ckind, rewritten))

    merged_nodes: dict[str, dict[str, Any]] = {}
    merged_edges: dict[str, dict[str, Any]] = {}
    quarantined_nodes: set[str] = set()
    quarantined_edges: set[str] = set()
    conflict_details: list[dict[str, Any]] = []

    # Index unshared/candidate nodes for alignment candidate generation (never auto-merged!).
    same_code_buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    column_label_buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)

    # Pass 1: collect definitions & all nodes from all contributors, retaining per-contributor id_remap.
    contributor_remaps: list[tuple[str, str, dict[str, Any], dict[str, str]]] = []
    for cid, ckind, graph in prepared_contributors:
        for category in ('entities', 'relations'):
            for defn in graph.get('definitions', {}).get(category, []):
                copied = deepcopy(defn)
                if category == 'entities':
                    copied.setdefault('display_mode', 'auto')
                    copied.setdefault('identity_origin', 'row')
                prev = definitions[category].get(copied['type'])
                if prev is None:
                    definitions[category][copied['type']] = copied
                elif prev != copied:
                    if ({k: v for k, v in prev.items() if k != 'description'}
                            != {k: v for k, v in copied.items() if k != 'description'}):
                        raise LightGraphError('published_definition_conflict')
                    if not prev.get('description') and copied.get('description'):
                        prev['description'] = copied['description']

        id_remap: dict[str, str] = {}
        for raw_node in graph.get('nodes', []):
            node = deepcopy(raw_node)
            orig_id = node['id']
            can_share = _node_shared_allowed(
                node,
                kind=ckind,
                shared_types=shared_types,
                base_node_ids=base_node_ids,
            )
            if not can_share:
                # Isolate cross-unit entities without declared shared identity domain.
                scoped_id = 'n_' + digest('unit_scoped', cid, orig_id)
                node['id'] = scoped_id
                node['unit_id'] = cid
                id_remap[orig_id] = scoped_id
            else:
                id_remap[orig_id] = orig_id

            target_id = node['id']
            existing = merged_nodes.get(target_id)
            if existing is None:
                merged_nodes[target_id] = node
            else:
                if (existing.get('type') != node.get('type')
                        or persisted_identity_key(existing) != persisted_identity_key(node)
                        or existing.get('identity_origin', 'row') != node.get('identity_origin', 'row')):
                    raise LightGraphError('published_graph_identity_conflict')
                if existing.get('identity_origin') == 'physical_column':
                    from .metadata_columns import merge_column_supports
                    merge_column_supports(existing, node)
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

        contributor_remaps.append((cid, ckind, graph, id_remap))

    # Pass 2: process all edges across contributors after all nodes are indexed.
    for cid, ckind, graph, id_remap in contributor_remaps:
        for raw_edge in graph.get('edges', []):
            edge = deepcopy(raw_edge)
            def resolve_endpoint(key):
                original = edge[key]
                if ckind == 'base':
                    # A frozen publication is self-contained. Its historical lookup
                    # witnesses describe how the publisher established this edge.
                    if original not in id_remap:
                        raise LightGraphError('published_relation_endpoint_unavailable')
                    return id_remap[original]
                scopes = {any(match.get('endpoint') == key and match.get('inherited_node_id') == original
                              for match in support.get('lookup_matches', []))
                          for support in edge.get('supports', [])}
                if True in scopes:
                    if original not in base_node_ids or not graph.get('extends_base_graph'):
                        raise LightGraphError('published_relation_endpoint_unavailable')
                    if False in scopes and id_remap.get(original, original) != original:
                        raise LightGraphError('ambiguous_inherited_identity')
                    return original
                if original in id_remap:
                    return id_remap[original]
                # Shared types can connect contributions that independently verify the same domain.
                node = merged_nodes.get(original)
                if node and node['type'] in shared_types:
                    return original
                raise LightGraphError('published_relation_endpoint_unavailable')
            src, dst = resolve_endpoint('source'), resolve_endpoint('target')
            edge['source'] = src
            edge['target'] = dst
            if src != raw_edge['source'] or dst != raw_edge['target']:
                edge['id'] = 'r_' + digest(edge['type'], src, dst)
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

    # Label conflict detection & authority resolution on merged nodes.
    for node_id, node in list(merged_nodes.items()):
        if node.get('identity_origin') == 'physical_column':
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
            if policy.conflict_mode == 'strict':
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
        if not rel_def.get('acyclic'):
            continue
        rel_type = rel_def['type']
        rel_edges = [e for e in merged_edges.values() if e['type'] == rel_type]
        cyclic_pairs = set()
        directed_topology(merged_nodes.keys(), rel_edges, cycle_edge_pairs=cyclic_pairs)
        if cyclic_pairs and policy.conflict_mode == 'strict':
            raise LightGraphError('published_relation_cycle')
        for edge in rel_edges:
            if (edge['source'], edge['target']) in cyclic_pairs:
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

    # Build alignment candidates WITHOUT auto-merging or auto-connecting!
    candidate_identity_merges: list[dict[str, Any]] = []
    candidate_column_alignments: list[dict[str, Any]] = []
    if policy.emit_alignment_candidates:
        for (etype, _), items in sorted(same_code_buckets.items()):
            distinct_nodes = sorted({i['node_id'] for i in items if i['node_id'] in merged_nodes})
            if len(distinct_nodes) > 1:
                candidate_identity_merges.append({
                    'status': 'candidate_only',
                    'reason': 'cross_unit_unverified_identity_domain',
                    'entity_type': etype,
                    'node_ids': distinct_nodes,
                    'contributors': sorted({i['contributor_id'] for i in items}),
                    'identity': items[0]['identity'],
                })
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
        'definitions': {k: list(v.values()) for k, v in definitions.items()},
        'nodes': sorted(merged_nodes.values(), key=lambda n: n['id']),
        'edges': sorted(merged_edges.values(), key=lambda e: e['id']),
        'evidence_refs': evidence_refs,
        'evidence_catalog': evidence_catalog,
        'validation_snapshots': validation_snapshots,
        'candidate_identity_merges': candidate_identity_merges,
        'candidate_column_alignments': candidate_column_alignments,
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
    if key_versions:
        result['source_key_semantics'] = next(iter(key_versions))
    if target_access_fingerprint:
        result['access_fingerprint'] = target_access_fingerprint
    return derive_display_graph(result, copy=False)


def merge_construction_graphs(
    contributors: list[dict[str, Any]],
    *,
    target_access_fingerprint: str | None = None,
    merge_policy: MergePolicyConfig | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Public entry point for deterministic cross-unit / cross-project graph merging."""
    policy = (
        merge_policy if isinstance(merge_policy, MergePolicyConfig)
        else MergePolicyConfig.model_validate(merge_policy or {})
    )
    return _merge_contributor_nodes_and_edges(
        contributors,
        policy=policy,
        target_access_fingerprint=target_access_fingerprint,
    )


def _shard_bucket(identifier: str) -> str:
    return f'shard_{int(digest(identifier)[:4], 16) % SHARD_COUNT:02x}.json'


def write_sharded_graph(construction_dir: str | Path, merged: dict[str, Any], *, context_fingerprint: str,
                        unit_attempts: dict[str, str] | None = None) -> dict[str, Any]:
    """Persist core topology in graph-core.yaml and shard heavy supports/provenance across JSON shards."""
    root = Path(construction_dir)
    # A pinned reader must see the old topology and its exact evidence even during another merge.
    publication = 'published/' + digest(merged)
    (root / publication / 'evidence-shards').mkdir(parents=True, exist_ok=True)

    buckets: dict[str, dict[str, dict[str, Any]]] = defaultdict(lambda: {'nodes': {}, 'edges': {}})
    core_nodes = []
    for node in merged.get('nodes', []):
        nid = node['id']
        bucket = _shard_bucket(nid)
        buckets[bucket]['nodes'][nid] = {
            'supports': node.get('supports', []),
            'provenance': node.get('provenance', []),
        }
        core_nodes.append({k: v for k, v in node.items() if k not in {'supports', 'provenance'}})

    core_edges = []
    for edge in merged.get('edges', []):
        eid = edge['id']
        bucket = _shard_bucket(eid)
        buckets[bucket]['edges'][eid] = {
            'supports': edge.get('supports', []),
            'provenance': edge.get('provenance', []),
        }
        core_edges.append({k: v for k, v in edge.items() if k not in {'supports', 'provenance'}})

    shard_files = {}
    for bucket, payload in sorted(buckets.items()):
        rel = f'{publication}/evidence-shards/{bucket}'
        atomic_text(root / rel, json.dumps(payload, ensure_ascii=False, separators=(',', ':')))
        shard_files[bucket] = rel

    core = {
        **{k: v for k, v in merged.items() if k not in {'nodes', 'edges'}},
        'storage_layout': 'sharded_v1',
        'evidence_shards': shard_files,
        'nodes': core_nodes,
        'edges': core_edges,
    }
    core_rel = f'{publication}/graph-core.yaml'
    write_yaml(root / core_rel, core)

    manifest_path = root / 'manifest.yaml'
    manifest = (
        safe_load_yaml(manifest_path.read_text(encoding='utf-8'))
        if manifest_path.exists()
        else {'format_version': 1, 'target': 'graph', 'drafts': {}, 'evidence': {}, 'evidence_history': {}, 'publications': {}}
    )
    manifest['current_graph'] = {
        'context_fingerprint': context_fingerprint,
        **({'workflow_units': unit_attempts} if unit_attempts is not None else {}),
        'access_fingerprint': merged.get('access_fingerprint'),
        'path': core_rel,
        'storage_layout': 'sharded_v1',
    }
    write_yaml(manifest_path, manifest)
    return {
        'core_path': core_rel,
        'shard_count': len(shard_files),
        'nodes': len(core_nodes),
        'edges': len(core_edges),
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
