"""Multi-slot hybrid retrieval with RRF fusion, bounded SQLite expansion, and faithful bridge overlay."""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Callable, Sequence
from itertools import combinations

from lightgraph.contracts import canonical, digest
from lightgraph.errors import LightGraphError
from lightgraph.construction.contexts import business_context_matches, normalize_context_selection, support_business_context
from .sync import active_index
from .store_sqlite import SQLiteGraphIndex, normalize_term
from .vector_local import Embedder, LocalHashEmbedder, LocalNumpyVectorStore, embedder_fingerprint

_SPLIT_RE = re.compile(r'[，,。；;、?!？！\n]+')
_PATH_STATE_LIMIT = 4096
_PATH_STATE_BYTES = 2 * 1024 * 1024
_PATH_OUTPUT_BYTES = 512 * 1024
_PATH_WITNESS_ATTEMPTS = 4096


def _join_contexts(left, right):
    """A path must satisfy every observed qualifier, never just graph adjacency."""
    dimensions = dict(left['dimensions'])
    types = dict(left['value_types'])
    for name, value in right['dimensions'].items():
        if name in dimensions and (types[name] != right['value_types'][name] or canonical(dimensions[name]) != canonical(value)):
            return None
        dimensions[name] = value
        types[name] = right['value_types'][name]
    status = ('unknown' if 'unknown' in (left['status'], right['status']) else
              'scoped' if dimensions else 'global')
    return {'status': status, 'dimensions': dict(sorted(dimensions.items())), 'value_types': types}


def _observation_groups(item):
    groups = {}
    for observation in item.get('observations') or [{}]:
        context = support_business_context(observation)
        context = {key: context[key] for key in ('status', 'dimensions', 'value_types')}
        # Row witnesses are alternatives, not interchangeable votes for a
        # business context. Keep them separate even when project/month agree.
        key = canonical([context, observation.get('row_context'),
                         (observation.get('provenance') or {}).get('source'),
                         (observation.get('provenance') or {}).get('context_columns')])
        groups.setdefault(key, (context, []))[1].append(observation)
    return [groups[key] for key in sorted(groups)]


def _row_witnesses(observations):
    result = {}
    for observation in observations:
        row = observation.get('row_context') or {}
        provenance = observation.get('provenance') or {}
        source, columns = provenance.get('source') or {}, provenance.get('context_columns')
        if (not row.get('id') or not source.get('source_id') or not source.get('table')
                or not isinstance(columns, list) or not columns
                or not all(isinstance(column, str) for column in columns)
                or not isinstance(row.get('values'), dict) or not set(columns) <= set(row['values'])):
            continue
        # Compare only declared, identical source-row grains. A different
        # table or a different column set is not evidence of a contradiction.
        domain = canonical([source['source_id'], source.get('schema', 'public'), source['table'], sorted(columns)])
        result[domain] = row['id']
    return result


def _join_rows(left, right):
    if any(domain in left and left[domain] != row_id for domain, row_id in right.items()):
        return None
    return {**left, **right}


def _context_paths(nodes, adjacency, seeds_by_slot, hops):
    """Bounded alternate witness paths; different contexts remain distinct.

    Neighborhood expansion supplies a union of individually visible facts. Only
    compatible edge and endpoint observations can support a path assertion.
    """
    all_paths, hit_slots, truncated = {}, defaultdict(set), False
    state_count, state_bytes, state_attempts = 0, 0, 0
    for slot, seeds in seeds_by_slot.items():
        if state_attempts >= _PATH_STATE_LIMIT:
            truncated = True
            break
        routes, seen, queue = defaultdict(list), set(), deque()

        def retain(node_id, context, rows, steps, visited):
            nonlocal truncated, state_count, state_bytes, state_attempts
            if state_attempts >= _PATH_STATE_LIMIT:
                truncated = True
                return
            state_attempts += 1
            # Store a fixed-size signature, never a second full copy of all
            # observation payloads in the deduplication set.
            serialized = canonical([node_id, context, rows, steps])
            key = digest(serialized)
            if key in seen:
                return
            size = len(serialized.encode('utf-8'))
            if (state_count >= _PATH_STATE_LIMIT or state_bytes + size > _PATH_STATE_BYTES
                    or len(routes[node_id]) >= 64):
                truncated = True
                return
            state_count += 1
            state_bytes += size
            seen.add(key)
            routes[node_id].append({'business_context': context, 'row_witnesses': rows, 'steps': steps})
            hit_slots[node_id].add(slot)
            queue.append((node_id, context, rows, steps, visited))

        for sid in seeds:
            if sid in nodes:
                for context, observations in _observation_groups(nodes[sid]):
                    retain(sid, context, _row_witnesses(observations), [], frozenset({sid}))
        while queue and state_attempts < _PATH_STATE_LIMIT:
            current, context, rows, steps, visited = queue.popleft()
            if len(steps) >= hops:
                continue
            for edge in adjacency[current]:
                if state_attempts >= _PATH_STATE_LIMIT:
                    truncated = True
                    break
                target = edge['target'] if edge['source'] == current else edge['source']
                if target in visited or target not in nodes:
                    continue
                for edge_context, observations in _observation_groups(edge):
                    if state_attempts >= _PATH_STATE_LIMIT:
                        truncated = True
                        break
                    joined = _join_contexts(context, edge_context)
                    joined_rows = _join_rows(rows, _row_witnesses(observations))
                    if joined is None or joined_rows is None:
                        continue
                    for target_context, target_observations in _observation_groups(nodes[target]):
                        if state_attempts >= _PATH_STATE_LIMIT:
                            truncated = True
                            break
                        applicable = _join_contexts(joined, target_context)
                        applicable_rows = _join_rows(joined_rows, _row_witnesses(target_observations))
                        if applicable is None or applicable_rows is None:
                            continue
                        refs = sorted({ref for observation in observations for ref in observation.get('evidence_refs', [])})
                        tables = sorted({table for observation in observations for table in observation.get('source_tables', [])})
                        step = {'edge_id': edge['id'], 'source': edge['source'], 'target': edge['target'],
                                'relation': edge['relation'],
                                'direction': 'outgoing' if edge['source'] == current else 'incoming',
                                'business_context': edge_context, 'observations': observations,
                                'evidence_refs': refs, 'source_tables': tables}
                        retain(target, applicable, applicable_rows, steps + [step], visited | {target})
        all_paths[slot] = dict(routes)
        if queue:
            truncated = True
    return all_paths, hit_slots, truncated


@dataclass(frozen=True)
class QuerySlot:
    surface: str
    normalized: str
    role_hint: str | None = None
    source: str = 'chunk'


def is_literal_span(text: str, start: int, end: int) -> bool:
    """Keep a Latin identifier whole, including when it touches Chinese text."""
    def word(char):
        return char.isascii() and (char.isalnum() or char == '_')

    return (end > start and not (start and word(text[start - 1]) and word(text[start]))
            and not (end < len(text) and word(text[end - 1]) and word(text[end])))


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
            if is_literal_span(norm_query, idx, end) and not any(occupied[idx:end]):
                for pos in range(idx, end):
                    occupied[pos] = True
                found.append((idx, end, traw, 'exact_term'))
                start = end
            else:
                start = idx + 1

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


def _rank_hits(slot, vec_hits, lex_hits, limit):
    """Fuse the requested channels and select the seeds for one text slot."""
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
    return scored[:limit]


class SubgraphRetriever:
    """Draft v1 hybrid retriever: scoped vector+lexical parallel slots, RRF, bounded expansion, faithful bridges."""

    def __init__(self, retrieval_dir: str | Path, *, embedder: Embedder | None = None,
                 expected_generation: str | None = None, analytics_loader=None, mention_extractor=None):
        self.root, manifest = active_index(Path(retrieval_dir), expected_generation)
        self.cohorts = manifest['cohorts']
        self.cohort_contexts = manifest['cohort_contexts']
        self.generation = manifest['generation']
        self.vector_dim = int(manifest.get('vector_dim') or 0)
        self.analytics_loader = analytics_loader
        self.mention_extractor = mention_extractor
        self.index = SQLiteGraphIndex(self.root / 'retrieval.sqlite')
        self.vectors = LocalNumpyVectorStore(self.root / 'vectors')
        self.embedder = embedder or LocalHashEmbedder()
        if manifest['embedder'] != embedder_fingerprint(self.embedder):
            raise LightGraphError('retrieval_index_stale')

    def authorized_cohorts(self, scopes, business_context=None):
        """Restrict all candidate scoring and traversal to permitted support slices."""
        selection = normalize_context_selection(business_context)
        allowed = set(scopes)
        return [token for token, required in self.cohorts.items()
                if allowed.issuperset(required) and business_context_matches(
                    {'business_context': self.cohort_contexts[token]}, selection)]

    def retrieve(
        self,
        query: str = '',
        *,
        node_id: str | None = None,
        field: str = 'all',
        method: str = 'hybrid',
        scopes: Sequence[str] | set[str] = ('public',),
        business_context: dict[str, Any] | None = None,
        top_n: int = 20,
        top_p_seeds: int = 4,
        hops: int | None = None,
        max_per_rel: int = 30,
        hub_degree_limit: int = 150,
        max_nodes: int = 80,
        max_edges: int = 150,
        extractor: Callable[[str], list[dict[str, Any]]] | None = None,
        resolved_conditions: Sequence[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        scope_list = sorted({str(s) for s in scopes})
        selection = normalize_context_selection(business_context)
        cohorts = self.authorized_cohorts(scope_list, selection)
        if field not in {'all', 'name', 'description'} or method not in {'keyword', 'vector', 'hybrid'}:
            raise ValueError('invalid_retrieval_mode')
        if hops is None:
            hops = 0 if node_id is not None else 2
        resolved = {c['text']: [hit['node_id'] for hit in c['candidates']] for c in resolved_conditions}
        if node_id is not None:
            resolved = {node_id: [node_id]}
            slots = [QuerySlot(node_id, node_id, source='node_id')]
        elif resolved:
            slots = [QuerySlot(c['text'], normalize_term(c['text']), c['role'], c.get('match', 'exact'))
                     for c in resolved_conditions]
        elif field == 'description' or method == 'vector':
            # Semantic sentences and description phrases are scored as supplied.
            # Splitting them on names found in another field changes their meaning.
            slots = [QuerySlot(query.strip(), normalize_term(query), source='full_query')] if query.strip() else []
        else:
            terms = self.index.authorized_terms(cohorts, field=field)
            slots = decompose_query(query, known_terms=terms, extractor=extractor)
        if not slots:
            return {
                'query': query,
                'scopes': scope_list,
                'business_context': selection,
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
        vec_batches = [[] for slot in slots]
        if not resolved and method in {'hybrid', 'vector'} and cohorts:
            slot_surfaces = [slot.surface for slot in slots]
            q_vectors = (
                self.embedder.embed_queries(slot_surfaces)
                if hasattr(self.embedder, 'embed_queries')
                else self.embedder.embed(slot_surfaces)
            )
            if self.vector_dim > 0 and q_vectors.ndim == 2 and q_vectors.shape[1] != self.vector_dim:
                raise LightGraphError('retrieval_index_stale')
            vec_batches = self.vectors.search_batch(q_vectors, scopes=cohorts, top_k=top_n, field=field)

        slot_results = []
        seed_pool: list[str] = []
        seed_set: set[str] = set()
        seeds_by_slot: dict[str, list[str]] = {}

        authorized = self.index.load_authorized_nodes(
            {nid for ids in resolved.values() for nid in ids}, scopes=cohorts) if resolved else {}
        for slot, vec_hits in zip(slots, vec_batches):
            if resolved:
                chosen = [{'node_id': nid, 'kind': authorized[nid]['kind'],
                           'match_type': 'exact_id' if node_id is not None else slot.source,
                           'rrf_score': None, 'lex_rank': None, 'vec_rank': None, 'vec_score': None}
                          for nid in resolved[slot.surface] if nid in authorized]
            else:
                lex_hits = (self.index.search_lexical(slot.surface, scopes=cohorts, limit=top_n, field=field)
                            if method in {'hybrid', 'keyword'} else [])
                chosen = _rank_hits(slot, vec_hits, lex_hits, top_p_seeds)
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

        slot_routes, node_hit_slots, path_truncated = _context_paths(nodes_map, adj, seeds_by_slot, hops)

        # A common endpoint is a bridge only when at least two real paths have
        # compatible conditions. Keep alternate witnesses rather than combining
        # different months or measures into one alleged proof.
        bridge_nodes = []
        path_output_bytes, witness_attempts = 2, 0
        for nid, hit_slots in sorted(node_hit_slots.items()):
            if witness_attempts >= _PATH_WITNESS_ATTEMPTS:
                path_truncated = True
                break
            groups, seen_groups = [], set()
            bridge_header = None
            for first, second in combinations(sorted(hit_slots), 2):
                for left in slot_routes[first][nid]:
                    for right in slot_routes[second][nid]:
                        if witness_attempts >= _PATH_WITNESS_ATTEMPTS:
                            path_truncated = True
                            break
                        witness_attempts += 1
                        if not left['steps'] and not right['steps']:
                            continue
                        context = _join_contexts(left['business_context'], right['business_context'])
                        rows = _join_rows(left['row_witnesses'], right['row_witnesses'])
                        if context is None or rows is None:
                            continue
                        witness = {'business_context': context,
                                   'context_status': 'unverified' if context['status'] == 'unknown' else 'compatible',
                                   'path_semantics': 'condition_compatible_evidence_chain',
                                   'observed_path': False, 'row_witnesses': rows,
                                   'paths_by_slot': {first: left['steps'], second: right['steps']}}
                        serialized = canonical(witness)
                        signature = digest(serialized)
                        if signature in seen_groups:
                            continue
                        if len(groups) >= 128:
                            path_truncated = True
                            break
                        header = bridge_header
                        if header is None:
                            node_info = nodes_map[nid]
                            header = {
                                'node_id': nid, 'kind': node_info['kind'], 'display_name': node_info['display_name'],
                                'bridge_role': 'intermediate_bridge' if nid not in seed_set else 'cross_slot_node',
                                'matched_slots': sorted(witness['paths_by_slot']),
                                'real_paths_by_slot': witness['paths_by_slot'], 'path_groups': [],
                                'context_status': witness['context_status'],
                                'path_semantics': witness['path_semantics'], 'observed_path': False,
                            }
                        # Include the primary path's duplicate preview and all
                        # bridge headers in the one query-wide output budget.
                        size = len(serialized.encode('utf-8')) + 1
                        if not groups:
                            size += len(canonical(header).encode('utf-8')) + 1
                        if path_output_bytes + size > _PATH_OUTPUT_BYTES:
                            path_truncated = True
                            continue
                        path_output_bytes += size
                        bridge_header = header
                        seen_groups.add(signature)
                        groups.append(witness)
                    if len(groups) >= 128 or witness_attempts >= _PATH_WITNESS_ATTEMPTS:
                        break
                if len(groups) >= 128 or witness_attempts >= _PATH_WITNESS_ATTEMPTS:
                    break
            if groups:
                bridge_nodes.append({**bridge_header, 'path_groups': groups})
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
            'business_context': selection,
            'slots': slot_results,
            'nodes': output_nodes,
            'edges': edges,
            'bridge_nodes': bridge_nodes,
            'connected_components': connected_components,
            'truncated': expanded['truncated'] or truncated_slots or path_truncated,
            'truncation_reasons': expanded['truncation_reasons'] + (['slot_limit_reached'] if truncated_slots else [])
                                  + (['path_budget_reached'] if path_truncated else []),
            'connected_components_semantics': 'visible_fact_union_not_context_proof',
            'hub_skipped_nodes': expanded['hub_skipped_nodes'],
        }
