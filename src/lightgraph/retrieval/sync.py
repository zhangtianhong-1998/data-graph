"""Sync scope-authorized published graphs into SQLite topology and local vector store."""
from __future__ import annotations

from collections import defaultdict
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from lightgraph.contracts import canonical, digest
from lightgraph.errors import LightGraphError
from lightgraph.runtime.retention import atomic_text
from lightgraph.construction.access import filter_graph_scopes
from .store_sqlite import SQLiteGraphIndex, normalize_term
from .vector_local import Embedder, LocalHashEmbedder, LocalNumpyVectorStore, embedder_fingerprint


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
        if manifest['format'] != 2 or (expected_generation is not None and manifest['generation'] != expected_generation):
            raise ValueError
        if not all((path / file).is_file() for file in ('retrieval.sqlite', 'vectors/vectors.npy', 'vectors/vectors_meta.sqlite')):
            raise ValueError
        return path, manifest
    except (OSError, ValueError, KeyError, TypeError):
        raise LightGraphError('retrieval_index_stale') from None


def sync_graph_to_retrieval(
    graph: dict[str, Any],
    retrieval_dir: str | Path,
    *,
    embedder: Embedder | None = None,
    generation: str | None = None,
) -> dict[str, Any]:
    """Index nodes/edges per scope_ref so restricted aliases never bias public vector scores."""
    root = Path(retrieval_dir)
    root.mkdir(parents=True, exist_ok=True)
    embedder = embedder or LocalHashEmbedder()

    # Each support requires its primary scope AND all lookup scopes. Never turn
    # that conjunction into a list of independent grants.
    requirements = set()
    for collection in ('nodes', 'edges'):
        for item in graph.get(collection, []):
            for support in item.get('supports', []):
                required = {support.get('scope_ref'), *support.get('required_scope_refs', [])}
                if all(isinstance(scope, str) for scope in required):
                    requirements.add(tuple(sorted(required)))
    if graph.get('support_visibility_version') != 1 and (graph.get('nodes') or graph.get('edges')):
        raise LightGraphError('graph_support_visibility_missing')
    cohorts = {canonical(scopes): scopes for scopes in sorted(requirements)}
    node_rows = {}
    edge_rows = []
    endpoint_ids = {node['id'] for node in graph.get('nodes', [])}
    scope_views: list[dict[str, Any]] = []
    terms: list[dict[str, Any]] = []
    vector_items: list[dict[str, Any]] = []

    for scope_ref, required_scopes in cohorts.items():
        scoped_graph = filter_graph_scopes(graph, required_scopes, external_visible_ids=endpoint_ids)
        scoped_nodes = {n['id']: n for n in scoped_graph['nodes']}
        neighbors_by_node: dict[str, list[str]] = defaultdict(list)
        for e in scoped_graph.get('edges', []):
            rel = e.get('type') or e.get('relation') or ''
            scopes, refs, tables = _edge_source_info(e)
            edge_rows.append({
                'edge_id': e['id'], 'scope_ref': scope_ref, 'src_id': e['source'], 'dst_id': e['target'],
                'rel_type': rel, 'weight': 1.0, 'qualifiers': e.get('properties') or {},
                'scopes': scopes, 'evidence_refs': refs, 'source_tables': tables,
            })
            src_node = scoped_nodes.get(e['source'])
            dst_node = scoped_nodes.get(e['target'])
            if src_node and dst_node:
                neighbors_by_node[e['source']].append(f'{rel} {dst_node.get("display_label") or dst_node.get("label") or ""}')
                neighbors_by_node[e['target']].append(f'{src_node.get("display_label") or src_node.get("label") or ""} {rel}')

        for node in scoped_graph['nodes']:
            nid = node['id']
            kind = node.get('type') or node.get('kind') or 'Entity'
            node_rows[nid] = {'node_id': nid, 'kind': kind, 'identity': node.get('identity') or {}}
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
            })

            seen_terms: set[tuple[str, str, int]] = set()
            def add_term(raw: Any, role: str, is_code: bool = False) -> None:
                raw_str = str(raw or '').strip()
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
            if chinese_name:
                add_term(chinese_name, 'chinese_name', False)
            if english_name:
                add_term(english_name, 'english_name', False)
            add_term(disp, 'display_name', False)
            for alias in aliases:
                add_term(alias, 'alias', False)

            code_str = ' '.join(str(v) for v in (node.get('identity') or {}).values())
            nbr_str = ' ; '.join(sorted(set(neighbors_by_node.get(nid, [])))[:5])
            card_parts = [
                f'[{kind}]',
                chinese_name or disp,
                f'英文:{english_name}' if english_name else '',
                f'编码:{code_str}' if code_str else '',
                f'别名:{" ".join(aliases)}' if aliases else '',
                f'描述:{description}' if description else '',
                f'关联:{nbr_str}' if nbr_str else '',
            ]
            card_text = ' | '.join(part for part in card_parts if part)
            vector_items.append({
                'node_id': nid,
                'kind': kind,
                'scope_ref': scope_ref,
                'card_text': card_text,
            })

    # A failed embedding or an overlapping sync cannot expose a partially
    # replaced matrix or pair it with another generation's SQLite row indices.
    gen_token = generation if generation is not None else digest(graph)
    model_tag = embedder_fingerprint(embedder)
    build_id = digest('retrieval-v2', gen_token, model_tag)[:32]
    builds = root / 'builds'
    builds.mkdir(exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.building-', dir=builds))
    try:
        index = SQLiteGraphIndex(staging / 'retrieval.sqlite')
        index.replace_index(nodes=list(node_rows.values()), scope_views=scope_views, terms=terms, edges=edge_rows)
        vec_store = LocalNumpyVectorStore(staging / 'vectors')
        vectors = embedder.embed([item['card_text'] for item in vector_items])
        vec_store.replace_all(vector_items, vectors)
        # Concurrent rebuilds of identical immutable inputs share one directory.
        try:
            staging.rename(builds / build_id)
        except OSError:
            if not (builds / build_id).is_dir():
                raise
        manifest = {'format': 2, 'generation': gen_token, 'build': build_id, 'cohorts': cohorts,
                    'embedder': model_tag}
        atomic_text(root / 'current.json', canonical(manifest))
    finally:
        if staging.exists():
            shutil.rmtree(staging)

    return {
        'status': 'synced',
        'generation': gen_token,
        'nodes_indexed': len(node_rows),
        'edges_indexed': len({row['edge_id'] for row in edge_rows}),
        'scope_views': len(scope_views),
        'vector_cards': len(vector_items),
        'vector_dim': int(vectors.shape[1]) if vectors.ndim == 2 and vectors.shape[0] else embedder.dim,
    }
