"""Sync scope-authorized published graphs into SQLite topology and local vector store."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
from contextlib import closing
import errno
import sqlite3
import shutil
import tempfile
import time
import numpy as np
from pathlib import Path
from typing import Any
from uuid import uuid4

from lightgraph.source_values import original_key_types, scalar_kind
from lightgraph.contracts import canonical, digest
from lightgraph.errors import LightGraphError
from lightgraph.runtime.retention import atomic_text
from lightgraph.construction.access import filter_graph_scopes
from lightgraph.construction.contexts import business_context_identity
from .store_sqlite import SQLiteGraphIndex, normalize_term, observed_text_properties
from .vector_local import Embedder, LocalNumpyVectorStore, embedder_fingerprint


def _publish_staging(staging: Path, destination: Path) -> None:
    """Retry transient Windows sharing locks before advancing the index pointer."""
    for attempt in range(11):
        try:
            staging.rename(destination)
            return
        except OSError as exc:
            transient = (getattr(exc, 'winerror', None) in {5, 32, 33}
                         or exc.errno in {errno.EACCES, errno.EBUSY, errno.EPERM})
            if not transient or attempt == 10:
                raise
            time.sleep(.05)


def _node_source_tables(node: dict[str, Any]) -> list[str]:
    tables = set()
    for s in node.get('supports', []):
        src = (s.get('provenance') or {}).get('source') if isinstance(s.get('provenance'), dict) else None
        if isinstance(src, dict) and src.get('table'):
            sid = src.get('source_id') or ''
            sch = src.get('schema') or 'public'
            tables.add(f'{sid}.{sch}.{src["table"]}' if sid else f'{sch}.{src["table"]}')
    for p in node.get('provenance', []):
        src = p.get('source') if isinstance(p, dict) else None
        if isinstance(src, dict) and src.get('table'):
            sid = src.get('source_id') or ''
            sch = src.get('schema') or 'public'
            tables.add(f'{sid}.{sch}.{src["table"]}' if sid else f'{sch}.{src["table"]}')
    return sorted(tables)


def _edge_source_info(edge: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    scopes, refs, tables = set(), set(), set()
    for s in edge.get('supports', []):
        if s.get('scope_ref'):
            scopes.add(str(s['scope_ref']))
        prov = s.get('provenance') if isinstance(s.get('provenance'), dict) else {}
        for ref in prov.get('evidence_refs') or []:
            refs.add(str(ref))
        for ref in s.get('evidence_refs') or []:
            refs.add(str(ref))
        src = prov.get('source') if isinstance(prov.get('source'), dict) else {}
        if src.get('table'):
            sid = src.get('source_id') or ''
            sch = src.get('schema') or 'public'
            tables.add(f'{sid}.{sch}.{src["table"]}' if sid else f'{sch}.{src["table"]}')
    if not scopes and edge.get('scopes'):
        scopes.update(str(x) for x in edge['scopes'])
    if not scopes:
        scopes.add('public')
    return sorted(scopes), sorted(refs), sorted(tables)


def _observations(item, key_names=()):
    """Keep each qualified assertion with its own values and supporting sources."""
    result = []
    for support in item.get('supports', []):
        scopes, refs, tables = _edge_source_info({'supports': [support]})
        ordered = {}
        origins = support.get('identity_origins') or []
        original = support.get('original_key_values') or {}
        columns = (support.get('provenance') or {}).get('key_columns') or []
        if original and columns and set(columns) == set(original):
            # Fusion can rename/reorder keys or normalize their printed values.
            # Recover the original driver type only if every matching origin agrees.
            recorded = support.get('original_key_types')
            if recorded is None and not support.get('identity_decision_refs'):
                recorded = original_key_types(original, origins)
            if (isinstance(recorded, dict) and set(columns) == set(recorded)
                    and all(isinstance(recorded[c], str) and recorded[c] for c in columns)):
                ordered['source_identity'] = {'values': [original[c] for c in columns],
                                              'value_types': [recorded[c] for c in columns]}
        elif key_names and not origins and set(key_names) == set(item.get('identity', {})):
            ordered['identity_key_names'] = list(key_names)
        result.append({**ordered, 'business_context': business_context_identity(support),
                       'properties': support.get('properties', {}),
                       'evidence_refs': refs, 'source_tables': tables, 'scopes': scopes,
                       **{key: support[key] for key in ('observation_id', 'label', 'description', 'english_name', 'property_observations',
                           'aliases', 'row_context', 'source_row_witness', 'provenance',
                           'execution_origins', 'lookup_matches', 'endpoint_origins', 'identity_origins', 'identity_basis',
                           'identity_decision_refs', 'identity_decision_members', 'identity_decision_endpoints',
                           'identity_approval_ref', 'reviewed_input_node_id',
                           'inherited_from', 'contributor_id', 'required_scope_refs') if key in support}})
    return result


def active_index(root: Path, expected_generation: str | None = None) -> tuple[Path, dict]:
    """Pin a complete index build before reading any topology, terms or vectors."""
    try:
        manifest = json.loads((root / 'current.json').read_text(encoding='utf-8'))
        build = manifest['build']
        if not isinstance(build, str) or len(build) != 32 or any(c not in '0123456789abcdef' for c in build):
            raise ValueError
        path = root / 'builds' / build
        if not isinstance(manifest['cohorts'], dict) or not isinstance(manifest['embedder'], str):
            raise ValueError
        generation = manifest['generation']
        if not isinstance(generation, str) or (expected_generation is not None and generation != expected_generation):
            raise ValueError
        if manifest['format'] not in (6, 7) or not isinstance(manifest['embedder_provider'], str):
            raise ValueError
        if not isinstance(manifest['cohort_contexts'], dict) or set(manifest['cohort_contexts']) != set(manifest['cohorts']):
            raise ValueError
        files = ['retrieval.sqlite']
        if manifest['format'] == 7:
            if manifest.get('embedder_provider') != 'none' or manifest['embedder'] != embedder_fingerprint(None):
                raise ValueError
        elif manifest.get('embedder_provider') != 'local_hash':
            files.extend(('vectors/vectors.npy', 'vectors/vectors_meta.sqlite'))
        if not all((path / file).is_file() for file in files):
            raise ValueError
        return path, manifest
    except (OSError, ValueError, KeyError, TypeError):
        raise LightGraphError('retrieval_index_stale') from None


def pin_active_index(root: Path, expected_generation=None):
    """Hold a shared read transaction for the lifetime of a reader of this build."""
    for _ in range(4):
        path, manifest = active_index(root, expected_generation)
        if not manifest.get('reader_leases'):
            return path, manifest, None
        lease = None
        try:
            lease = sqlite3.connect((path / 'readers.sqlite').resolve().as_uri() + '?mode=ro', uri=True,
                                    check_same_thread=False)
            lease.execute('BEGIN')
            lease.execute('SELECT * FROM readers').fetchall()
            if json.loads((root / 'current.json').read_text(encoding='utf-8')).get('build') == manifest['build']:
                return path, manifest, lease
        except sqlite3.OperationalError:
            pass
        if lease:
            lease.close()
    raise LightGraphError('retrieval_index_stale')


def _publish_index(root, staging, manifest):
    """Publish under a writer lock. Old clients without reader leases keep their legacy builds."""
    builds = root.resolve() / 'builds'
    pointer = root / 'current.json'
    with closing(sqlite3.connect(staging / 'readers.sqlite')) as db:
        db.execute('CREATE TABLE readers (id INTEGER)')
    with closing(sqlite3.connect(root / 'publication.sqlite', timeout=30)) as publisher:
        publisher.execute('BEGIN IMMEDIATE')
        try:
            previous = json.loads(pointer.read_text(encoding='utf-8'))
        except (FileNotFoundError, ValueError):
            previous = {}
        _publish_staging(staging, builds / manifest['build'])
        atomic_text(pointer, canonical(manifest))
        # Keep the current build and one predecessor for inspection. A running
        # query can keep any older build alive, including in another process.
        for path in builds.iterdir():
            if path.name in {manifest['build'], (previous.get('build') if isinstance(previous, dict) else None)} or len(path.name) != 32:
                continue
            marker = path / 'readers.sqlite'
            if path.is_symlink() or not marker.is_file() or marker.is_symlink():
                continue
            try:
                with closing(sqlite3.connect(marker.as_uri() + '?mode=rw', uri=True, timeout=0)) as marker_db:
                    marker_db.execute('BEGIN EXCLUSIVE')
                shutil.rmtree(path)
            except (sqlite3.Error, OSError):
                continue


def index_matches_embedder(manifest, embedder):
    # Hash indexes retain their scoped SQLite terms and topology. Their vectors
    # must never contribute a rank, even if the old files are still on disk.
    if manifest.get('format') == 6 and manifest.get('embedder_provider') == 'local_hash':
        return embedder is None
    return manifest['embedder'] == embedder_fingerprint(embedder)


def _embed_cards(embedder, texts, progress):
    if progress is None or not texts:
        return embedder.embed(texts)
    total = len(texts)
    batch_size = getattr(embedder, 'batch_size', 128)
    matrix = None
    progress('embedding', 0, total)
    for start in range(0, total, batch_size):
        end = min(start + batch_size, total)
        batch = embedder.embed(texts[start:end])
        if batch.ndim != 2 or len(batch) != end - start:
            raise ValueError('vector_count_mismatch')
        if matrix is None:
            matrix = np.empty((total, batch.shape[1]), dtype=batch.dtype)
        if matrix.shape[1] != batch.shape[1]:
            raise ValueError('vector_dimension_mismatch')
        matrix[start:end] = batch
        progress('embedding', end, total)
    return matrix


def sync_graph_to_retrieval(
    graph: dict[str, Any],
    retrieval_dir: str | Path,
    *,
    embedder: Embedder | None = None,
    generation: str | None = None,
    progress=None,
) -> dict[str, Any]:
    """Index each permission/context slice before producing terms or embeddings.

    Context is applicability, not authorization. The query independently selects
    eligible slices on both axes before ranking and every graph traversal.
    """
    root = Path(retrieval_dir)
    root.mkdir(parents=True, exist_ok=True)

    if graph.get('support_visibility_version') != 1 and (graph.get('nodes') or graph.get('edges')):
        raise LightGraphError('graph_support_visibility_missing')
    # One exact slice per support keeps a shared entity's K-only names,
    # descriptions, properties and edges out of its M embedding entirely.
    # Permission conjunctions remain independent of business context values.
    cohorts, cohort_contexts, cohort_graphs = {}, {}, {}
    for collection in ('nodes', 'edges'):
        for item in graph.get(collection, []):
            grouped = defaultdict(list)
            for support in item.get('supports', []):
                required = {support.get('scope_ref'), *support.get('required_scope_refs', [])}
                if all(isinstance(scope, str) for scope in required):
                    context = business_context_identity(support)
                    token = digest('retrieval-support-cohort-v1', sorted(required), context)
                    cohorts[token] = sorted(required)
                    cohort_contexts[token] = context
                    grouped[token].append(support)
            for token, supports in grouped.items():
                shard = cohort_graphs.setdefault(token, {
                    'nodes': [], 'edges': [], 'support_visibility_version': 1,
                    'definitions': graph.get('definitions', {}),
                    'label_conflict_policy': graph.get('label_conflict_policy'),
                })
                shard[collection].append({**item, 'supports': supports})
    node_rows = {}
    edge_rows = []
    endpoint_ids = {node['id'] for node in graph.get('nodes', [])}
    scope_views: list[dict[str, Any]] = []
    terms: list[dict[str, Any]] = []
    vector_items: list[dict[str, Any]] = []

    key_names = {d['type']: d.get('key_names', []) for d in graph.get('definitions', {}).get('entities', [])}
    for scope_ref, required_scopes in cohorts.items():
        scoped_graph = filter_graph_scopes(cohort_graphs[scope_ref], required_scopes,
                                           external_visible_ids=endpoint_ids)
        scoped_nodes = {n['id']: n for n in scoped_graph['nodes']}
        neighbors_by_node: dict[str, list[str]] = defaultdict(list)
        for e in scoped_graph.get('edges', []):
            rel = e.get('type') or e.get('relation') or ''
            scopes, refs, tables = _edge_source_info(e)
            edge_rows.append({
                'edge_id': e['id'], 'scope_ref': scope_ref, 'src_id': e['source'], 'dst_id': e['target'],
                'rel_type': rel, 'weight': 1.0, 'qualifiers': e.get('properties') or {},
                'scopes': scopes, 'evidence_refs': refs, 'source_tables': tables,
                'observations': _observations(e),
            })
            src_node = scoped_nodes.get(e['source'])
            dst_node = scoped_nodes.get(e['target'])
            if embedder is not None and src_node and dst_node:
                neighbors_by_node[e['source']].append(f'{rel} {dst_node.get("display_label") or dst_node.get("label") or ""}')
                neighbors_by_node[e['target']].append(f'{src_node.get("display_label") or src_node.get("label") or ""} {rel}')

        for node in scoped_graph['nodes']:
            nid = node['id']
            kind = node.get('type') or node.get('kind') or 'Entity'
            identity = node.get('identity') or {}
            node_rows[nid] = {'node_id': nid, 'kind': kind, 'identity': identity,
                              'identity_types': node.get('identity_types') or {k: scalar_kind(v) for k, v in identity.items()}}
            cn_names = sorted({
                str(s['label']).strip() for s in node.get('supports', [])
                if isinstance(s.get('label'), str) and s['label'].strip()
            })
            en_names = sorted({
                str(s['english_name']).strip() for s in node.get('supports', [])
                if isinstance(s.get('english_name'), str) and s['english_name'].strip()
            })
            descs = sorted({
                str(s['description']).strip() for s in node.get('supports', [])
                if isinstance(s.get('description'), str) and s['description'].strip()
            })
            disp = str(node.get('display_label') or node.get('label') or nid)
            aliases = list(node.get('aliases') or [])
            src_tables = _node_source_tables(node)
            chinese_name = cn_names[0] if cn_names else (disp if node.get('label_origin') == 'source' else None)
            english_name = en_names[0] if en_names else None
            description = '；'.join(descs) if descs else None

            observations = _observations(node, key_names.get(kind))
            scope_views.append({
                'node_id': nid,
                'scope_ref': scope_ref,
                'scopes': node['scopes'],
                'chinese_name': chinese_name,
                'english_name': english_name,
                'display_name': disp,
                'aliases': aliases,
                'description': description,
                'source_tables': src_tables,
                'observations': observations,
            })

            seen_terms: set[tuple[str, str, int]] = set()
            def add_term(raw: Any, role: str, is_code: bool = False) -> None:
                raw_str = ('' if raw is None else str(raw)).strip()
                norm = normalize_term(raw_str)
                if not norm:
                    return
                key = (norm, role, int(is_code))
                if key in seen_terms:
                    return
                seen_terms.add(key)
                terms.append({
                    'term_norm': norm,
                    'term_raw': raw_str,
                    'node_id': nid,
                    'kind': kind,
                    'role': role,
                    'scope_ref': scope_ref,
                    'is_exact_code': is_code,
                })

            for val in (node.get('identity') or {}).values():
                add_term(val, 'identity_code', True)
            for observation in observations:
                if observation.get('identity_decision_refs'):
                    for value in (observation.get('source_identity') or {}).get('values', []):
                        add_term(value, 'identity_code', True)
            for name in cn_names:
                add_term(name, 'chinese_name')
            for name in en_names:
                add_term(name, 'english_name')
            for desc in descs:
                add_term(desc, 'description')
            for support in node.get('supports', []):
                for _, raw in observed_text_properties(support):
                    add_term(raw, 'source_property')
            add_term(disp, 'display_name', False)
            for alias in aliases:
                add_term(alias, 'alias', False)

            if embedder is None:
                continue
            code_str = ' '.join(str(v) for v in (node.get('identity') or {}).values())
            nbr_str = ' ; '.join(sorted(set(neighbors_by_node.get(nid, []))))[:512]
            # Raw attributes can contain source records. Export their field names only;
            # approved label and description observations already supply semantic values.
            properties = sorted({str(name) for support in node.get('supports', [])
                                 for name in support.get('properties', {})})
            card_parts = [
                f'[{kind}]',
                chinese_name or disp,
                f'英文:{english_name}' if english_name else '',
                f'编码:{code_str}' if code_str else '',
                f'别名:{" ".join(aliases)}' if aliases else '',
                f'描述:{description}' if description else '',
                '属性:' + '；'.join(properties)[:1024] if properties else '',
                f'关联:{nbr_str}' if nbr_str else '',
            ]
            card_text = ' | '.join(part for part in card_parts if part)
            names = dict.fromkeys([disp, *cn_names, *en_names, *aliases])
            for field, content in [('all', card_text), ('name', ' | '.join(names)), ('description', description)]:
                if content:
                    vector_items.append({'node_id': nid, 'kind': kind, 'scope_ref': scope_ref,
                                         'field': field, 'card_text': content[:4096]})

    # A failed embedding or an overlapping sync cannot expose a partially
    # replaced matrix or pair it with another generation's SQLite row indices.
    gen_token = generation if generation is not None else digest(graph)
    model_tag = embedder_fingerprint(embedder)
    # A manual rebuild must publish its new vectors even when a service has
    # changed behind the same model name and URL. Existing readers keep their
    # immutable build while the pointer switches atomically after success.
    build_id = uuid4().hex
    builds = root / 'builds'
    builds.mkdir(exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.building-', dir=builds))
    try:
        index = SQLiteGraphIndex(staging / 'retrieval.sqlite')
        index.replace_index(nodes=list(node_rows.values()), scope_views=scope_views, terms=terms, edges=edge_rows,
                            relation_definitions=graph.get('definitions', {}).get('relations', []))
        resolved_dim = 0
        unique_texts = 0
        if embedder is not None:
            vec_store = LocalNumpyVectorStore(staging / 'vectors')
            # Identical names and descriptions often recur across support slices.
            # Reuse their model output while retaining every slice's own permissions.
            positions = {}
            row_positions = []
            for item in vector_items:
                content = item['card_text']
                if content not in positions:
                    positions[content] = len(positions)
                row_positions.append(positions[content])
            vectors = _embed_cards(embedder, list(positions), progress)
            if vectors.ndim != 2 or vectors.shape[0] != len(positions):
                raise ValueError('vector_count_mismatch')
            if len(positions) != len(vector_items):
                vectors = vectors[row_positions]
            vec_store.replace_all(vector_items, vectors)
            resolved_dim = int(vectors.shape[1]) if vectors.ndim == 2 and vectors.shape[0] else int(embedder.dim or 0)
            unique_texts = len(positions)
        if progress:
            progress('publishing', unique_texts, unique_texts)
        synced_at = datetime.now(timezone.utc).isoformat()
        embedder_provider = 'none'
        embedder_model = None
        if embedder is not None:
            embedder_provider = 'openai_compatible' if hasattr(embedder, 'base_url') else 'custom'
            embedder_model = getattr(embedder, 'model', type(embedder).__name__)
        nodes_indexed = len(node_rows)
        edges_indexed = len({row['edge_id'] for row in edge_rows})
        manifest = {
            'format': 7 if embedder is None else 6,
            'generation': gen_token,
            'build': build_id,
            'reader_leases': True,
            'cohorts': cohorts,
            'cohort_contexts': cohort_contexts,
            'embedder': model_tag,
            'embedder_model': embedder_model,
            'embedder_provider': embedder_provider,
            'vector_dim': resolved_dim,
            'nodes_indexed': nodes_indexed,
            'edges_indexed': edges_indexed,
            'vector_cards': len(scope_views) if embedder is not None else 0,
            'vector_rows': len(vector_items),
            'synced_at': synced_at,
        }
        _publish_index(root, staging, manifest)
    finally:
        if staging.exists():
            # Cleanup must not replace the original publication error if the
            # same external lock still holds files in the abandoned staging dir.
            shutil.rmtree(staging, ignore_errors=True)

    return {
        'status': 'synced',
        'vectorized': embedder is not None,
        'indexed': True,
        'generation': gen_token,
        'build': build_id,
        'nodes_indexed': nodes_indexed,
        'edges_indexed': edges_indexed,
        'scope_views': len(scope_views),
        'vector_cards': len(scope_views) if embedder is not None else 0,
        'vector_rows': len(vector_items),
        'vector_dim': resolved_dim,
        'embedder_model': embedder_model,
        'embedder_provider': embedder_provider,
        'synced_at': synced_at,
    }
