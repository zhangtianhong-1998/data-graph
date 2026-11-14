"""Multi-slot hybrid retrieval with RRF fusion, bounded SQLite expansion, and faithful bridge overlay."""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Callable, Sequence

from lightgraph.errors import LightGraphError
from .sync import active_index
from .store_sqlite import SQLiteGraphIndex, normalize_term
from .vector_local import Embedder, LocalHashEmbedder, LocalNumpyVectorStore, embedder_fingerprint

_SPLIT_RE = re.compile(r'[，,。；;、\s\?\!？！/\\|]+|(?:以及|并且|还有|或者|与|和|跟|及)')
_NOISE_TOKENS = frozenset({'请问', '帮我', '查询', '看一下', '看看', '什么', '怎么', '如何', '有哪些', '为什么', '的', '了', '吗', '呢', '里', '中'})


@dataclass(frozen=True)
class QuerySlot:
    surface: str
    normalized: str
    role_hint: str | None = None
    source: str = 'chunk'


def decompose_query(
    query: str,
    *,
    known_terms: Sequence[str] = (),
    extractor: Callable[[str], list[dict[str, Any]]] | None = None,
) -> list[QuerySlot]:
    """Decompose complex query into parallel retrieval slots using optional extractor or dictionary+chunking."""
    if extractor is not None:
        extracted = extractor(query)
        slots = []
        seen = set()
        for item in extracted:
            surf = str(item.get('surface') or '').strip()
            norm = normalize_term(surf)
            if norm and norm not in seen:
                seen.add(norm)
                slots.append(QuerySlot(surface=surf, normalized=norm, role_hint=item.get('role_hint'), source='extractor'))
        if slots:
            return slots

    norm_query = normalize_term(query)
    if not norm_query:
        return []

    found: list[tuple[int, int, str, str]] = []
    # Greedy longest-match scan over authorized dictionary terms (codes & names >= 2 chars).
    sorted_terms = sorted(
        {(normalize_term(t), t) for t in known_terms if len(normalize_term(t)) >= 2},
        key=lambda pair: (-len(pair[0]), pair[0]),
    )
    occupied = [False] * len(norm_query)
    for tnorm, traw in sorted_terms:
        start = 0
        while True:
            idx = norm_query.find(tnorm, start)
            if idx < 0:
                break
            end = idx + len(tnorm)
            if not any(occupied[idx:end]):
                for pos in range(idx, end):
                    occupied[pos] = True
                found.append((idx, end, traw, 'exact_term'))
            start = end

    # Capture remaining unmatched text chunks so semantic vector retrieval covers OOV phrases.
    residual_parts: list[str] = []
    buf: list[str] = []
    for idx, ch in enumerate(norm_query):
        if occupied[idx]:
            if buf:
                residual_parts.append(''.join(buf))
                buf.clear()
        else:
            buf.append(ch)
    if buf:
        residual_parts.append(''.join(buf))

    slots_ordered: list[tuple[int, QuerySlot]] = [(pos, QuerySlot(surface=raw, normalized=normalize_term(raw), source=src))
                                                 for pos, _end, raw, src in found]
    seen_norms = {slot.normalized for _, slot in slots_ordered}

    offset = 100000
    for part in residual_parts:
        for chunk in _SPLIT_RE.split(part):
            cleaned = chunk.strip()
            for noise in _NOISE_TOKENS:
                cleaned = cleaned.replace(noise, ' ').strip()
            norm = normalize_term(cleaned)
            if len(norm) >= 2 and norm not in seen_norms:
                seen_norms.add(norm)
                slots_ordered.append((offset, QuerySlot(surface=cleaned, normalized=norm, source='chunk')))
                offset += 1

    if not slots_ordered:
        return [QuerySlot(surface=query.strip(), normalized=norm_query, source='full_query')]

    slots_ordered.sort(key=lambda item: item[0])
    unique = {}
    for _, slot in slots_ordered:
        unique.setdefault(slot.normalized, slot)
    return list(unique.values())


class SubgraphRetriever:
    """Draft v1 hybrid retriever: scoped vector+lexical parallel slots, RRF, bounded expansion, faithful bridges."""

    def __init__(self, retrieval_dir: str | Path, *, embedder: Embedder | None = None,
                 expected_generation: str | None = None):
        self.root, manifest = active_index(Path(retrieval_dir), expected_generation)
        self.cohorts = manifest['cohorts']
        self.generation = manifest['generation']
        self.index = SQLiteGraphIndex(self.root / 'retrieval.sqlite')
        self.vectors = LocalNumpyVectorStore(self.root / 'vectors')
        self.embedder = embedder or LocalHashEmbedder()
        if manifest['embedder'] != embedder_fingerprint(self.embedder):
            raise LightGraphError('retrieval_index_stale')

    def retrieve(
        self,
        query: str,
        *,
        scopes: Sequence[str] | set[str] = ('public',),
        top_n: int = 20,
        top_p_seeds: int = 4,
        hops: int = 2,
        max_per_rel: int = 30,
        hub_degree_limit: int = 150,
        max_nodes: int = 80,
        max_edges: int = 150,
        extractor: Callable[[str], list[dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        scope_list = sorted({str(s) for s in scopes})
        allowed = set(scope_list)
        cohorts = [token for token, required in self.cohorts.items() if allowed.issuperset(required)]
        known_terms = self.index.authorized_terms(cohorts)
        slots = decompose_query(query, known_terms=known_terms, extractor=extractor)
        if not slots:
            return {
                'query': query,
                'scopes': scope_list,
                'slots': [],
                'nodes': [],
                'edges': [],
                'bridge_nodes': [],
                'connected_components': [],
                'truncated': False,
                'truncation_reasons': [],
            }

        # Keep a long or repetitive question from allocating an unbounded
        # query-by-candidate score matrix.
        max_slots = 32
        truncated_slots = len(slots) > max_slots
        slots = slots[:max_slots]
        slot_surfaces = [slot.surface for slot in slots]
        q_vectors = self.embedder.embed(slot_surfaces)
        vec_batches = self.vectors.search_batch(q_vectors, scopes=cohorts, top_k=top_n)

        slot_results = []
        seed_pool: list[str] = []
        seed_set: set[str] = set()
        seeds_by_slot: dict[str, list[str]] = {}

        for slot, vec_hits in zip(slots, vec_batches):
            lex_hits = self.index.search_lexical(slot.surface, scopes=cohorts, limit=top_n)
            vec_ranks = {h['node_id']: (rank + 1, h) for rank, h in enumerate(vec_hits)}
            lex_ranks = {h['node_id']: (rank + 1, h) for rank, h in enumerate(lex_hits)}
            all_ids = sorted(set(vec_ranks) | set(lex_ranks))

            scored = []
            for nid in all_ids:
                vr, vh = vec_ranks.get(nid, (None, None))
                lr, lh = lex_ranks.get(nid, (None, None))
                rrf = (1.0 / (60 + vr) if vr is not None else 0.0) + (1.2 / (60 + lr) if lr is not None else 0.0)
                if lh and lh.get('is_exact_code'):
                    rrf += 0.03
                elif lh and lh.get('is_exact_name'):
                    rrf += 0.015
                kind = (lh or vh or {}).get('kind')
                if slot.role_hint and kind == slot.role_hint:
                    rrf += 0.015
                scored.append({
                    'node_id': nid,
                    'kind': kind,
                    'rrf_score': round(rrf, 6),
                    'vec_rank': vr,
                    'vec_score': round(vh['score'], 4) if vh else None,
                    'lex_rank': lr,
                    'match_type': lh['match_type'] if lh else None,
                })
            scored.sort(key=lambda x: (-x['rrf_score'], x['node_id']))
            chosen = scored[:top_p_seeds]
            seeds_by_slot[slot.surface] = [c['node_id'] for c in chosen]
            for item in chosen:
                if item['node_id'] not in seed_set:
                    seed_set.add(item['node_id'])
                    seed_pool.append(item['node_id'])
            slot_results.append({
                'surface': slot.surface,
                'role_hint': slot.role_hint,
                'source': slot.source,
                'candidates': chosen,
            })

        expanded = self.index.expand_neighborhood(
            seed_pool,
            scopes=cohorts,
            hops=hops,
            max_per_rel=max_per_rel,
            hub_degree_limit=hub_degree_limit,
            max_nodes=max_nodes,
            max_edges=max_edges,
        )

        nodes_map = expanded['nodes']
        edges = expanded['edges']

        # Compute per-slot reachability on the retrieved real subgraph (no fabricated paths!).
        adj: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for e in edges:
            adj[e['source']].append(e)
            adj[e['target']].append(e)

        slot_reach_paths: dict[str, dict[str, list[dict[str, Any]]]] = {}
        node_hit_slots: dict[str, set[str]] = defaultdict(set)

        for slot_name, slot_seeds in seeds_by_slot.items():
            visited_paths: dict[str, list[dict[str, Any]]] = {}
            q = deque()
            for sid in slot_seeds:
                if sid in nodes_map and sid not in visited_paths:
                    visited_paths[sid] = []
                    node_hit_slots[sid].add(slot_name)
                    q.append((sid, 0))
            while q:
                cur, depth = q.popleft()
                if depth >= hops:
                    continue
                for e in adj[cur]:
                    nxt = e['target'] if e['source'] == cur else e['source']
                    if nxt not in visited_paths:
                        visited_paths[nxt] = visited_paths[cur] + [{
                            'edge_id': e['id'],
                            'source': e['source'],
                            'target': e['target'],
                            'relation': e['relation'],
                            'direction': 'outgoing' if e['source'] == cur else 'incoming',
                            'evidence_refs': e['evidence_refs'],
                            'source_tables': e['source_tables'],
                        }]
                        node_hit_slots[nxt].add(slot_name)
                        q.append((nxt, depth + 1))
            slot_reach_paths[slot_name] = visited_paths

        # Identify bridge nodes reached by >= 2 distinct query slots via real edges.
        bridge_nodes = []
        for nid, hit_slots in sorted(node_hit_slots.items()):
            if len(hit_slots) >= 2 and nid in nodes_map and any(slot_reach_paths[name][nid] for name in hit_slots):
                node_info = nodes_map[nid]
                bridge_nodes.append({
                    'node_id': nid,
                    'kind': node_info['kind'],
                    'display_name': node_info['display_name'],
                    'bridge_role': 'intermediate_bridge' if nid not in seed_set else 'cross_slot_node',
                    'matched_slots': sorted(hit_slots),
                    'real_paths_by_slot': {
                        sname: slot_reach_paths[sname][nid]
                        for sname in sorted(hit_slots)
                    },
                })
        bridge_nodes.sort(key=lambda b: (0 if b['bridge_role'] == 'intermediate_bridge' else 1, -len(b['matched_slots']), b['node_id']))

        # Compute undirected connected components so disconnected query targets are explicit.
        comp_visited: set[str] = set()
        connected_components: list[list[str]] = []
        for nid in sorted(nodes_map):
            if nid in comp_visited:
                continue
            comp = []
            q = deque([nid])
            comp_visited.add(nid)
            while q:
                cur = q.popleft()
                comp.append(cur)
                for e in adj[cur]:
                    nxt = e['target'] if e['source'] == cur else e['source']
                    if nxt in nodes_map and nxt not in comp_visited:
                        comp_visited.add(nxt)
                        q.append(nxt)
            connected_components.append(sorted(comp))

        bridge_ids = {bridge['node_id'] for bridge in bridge_nodes}
        output_nodes = []
        for nid in sorted(nodes_map):
            n = dict(nodes_map[nid])
            slots_for_node = sorted(node_hit_slots.get(nid, set()))
            n['matched_slots'] = slots_for_node
            n['is_seed'] = nid in seed_set
            n['is_bridge'] = nid in bridge_ids
            output_nodes.append(n)

        return {
            'query': query,
            'scopes': scope_list,
            'slots': slot_results,
            'nodes': output_nodes,
            'edges': edges,
            'bridge_nodes': bridge_nodes,
            'connected_components': connected_components,
            'truncated': expanded['truncated'] or truncated_slots,
            'truncation_reasons': expanded['truncation_reasons'] + (['slot_limit_reached'] if truncated_slots else []),
            'hub_skipped_nodes': expanded['hub_skipped_nodes'],
        }
