"""Rank related entities and corroborate independent paraphrases in the visible index."""
from __future__ import annotations

import numpy as np

from lightgraph.errors import LightGraphError
from .store_sqlite import normalize_term


# A response-size budget, independent of any model's score scale.
_CANDIDATES = 5


def _vector_neighbors(retriever, fragments, cohorts, proposed_ids):
    """Return per-mention neighbors and unique first places in each semantic field."""
    unavailable = [None] * len(fragments)
    if retriever.embedder is None:
        return unavailable
    try:
        vectors = np.asarray(retriever.embedder.embed(fragments))
        if vectors.ndim != 2 or len(vectors) != len(fragments) or not vectors.shape[1]:
            raise LightGraphError('embedding_response_invalid')
        if retriever.vector_dim and vectors.shape[1] != retriever.vector_dim:
            raise LightGraphError('retrieval_index_stale')
    except LightGraphError as error:
        if not error.code.startswith('embedding_'):
            raise
        return unavailable

    valid = np.flatnonzero(np.isfinite(vectors).all(axis=1) & vectors.any(axis=1))
    if not len(valid):
        return unavailable
    neighbors = [({}, {}) for _ in valid]
    # Names and descriptions represent the entity itself. A combined card can
    # match a neighboring entity's name and cannot corroborate the link.
    eligible_names = [proposed_ids[index] for index in valid]
    for field in ('name', 'description'):
        batches = retriever.vectors.search_batch(vectors[valid], scopes=cohorts, top_k=_CANDIDATES + 1,
                                                 field=field, positive_only=False, with_peer_reference=True)
        for (ranked, leaders), hits, proposals in zip(neighbors, batches, eligible_names):
            # Flat scores in a field, including a singleton, do not distinguish
            # any entity. Never display an arbitrary subset of a tied group.
            if len(hits) < 2 or hits[0]['score'] == hits[-1]['score']:
                continue
            for hit in hits[:_CANDIDATES]:
                if hit['score'] == hits[-1]['score']:
                    break
                # A distant query also has a nearest node. Keep a proposal
                # only if the query enters its local graph neighborhood.
                nid = hit['node_id']
                is_leader = nid == hits[0]['node_id'] and hit['score'] > max(hits[1]['score'], 0)
                confirmed_name = is_leader and proposals == {nid}
                if not confirmed_name and (hit['peer_score'] is None or hit['score'] < hit['peer_score']):
                    continue
                if is_leader:
                    leaders.setdefault(nid, []).append(field)
                if nid not in ranked or ranked[nid]['score'] < hit['score']:
                    ranked[nid] = {'node_id': nid, 'kind': hit['kind'], 'score': hit['score'],
                                   'peer_score': round(hit['peer_score'], 5), 'matched_field': field,
                                   'evidence': 'vector_neighbor'}
    for index, result in zip(valid, neighbors):
        unavailable[index] = result
    return unavailable


def link_mentions(retriever, fragments, cohorts, *, paraphrases=None):
    """Vector neighbors suggest referents. A blind paraphrase can corroborate a unique graph name.

    Paraphrases originate from the question, before the proposer has seen any
    candidates. An inferred link is auditable via its matched terms and field.
    """
    results = [{'text': text, 'status': 'unmatched', 'candidates': []} for text in fragments]
    if not results or not cohorts:
        return results
    alternatives = {normalize_term(text): names for text, names in (paraphrases or {}).items()}
    terms = [list(dict.fromkeys(name for name in alternatives.get(normalize_term(text), ())
                                if name.strip() and normalize_term(name) != normalize_term(text))) for text in fragments]
    exact = retriever.index.exact_names((name for group in terms for name in group), scopes=cohorts,
                                        limit_per_term=_CANDIDATES)
    proposed_ids = [{hit['node_id'] for name in names for hit in exact.get(normalize_term(name), ())}
                    for names in terms]
    neighbors = _vector_neighbors(retriever, fragments, cohorts, proposed_ids)
    for result, names, vector_hits in zip(results, terms, neighbors):
        ranked, leaders = vector_hits if vector_hits is not None else ({}, {})
        matched_ids = set()
        all_rewrites_resolved = True
        for name in names:
            matches = exact.get(normalize_term(name), ())
            if not matches:
                all_rewrites_resolved = False
            for hit in matches:
                nid = hit['node_id']
                matched_ids.add(nid)
                candidate = ranked.setdefault(nid, {**hit, 'score': None, 'matched_field': 'name',
                                                     'evidence': 'proposed_name'})
                candidate.setdefault('matched_terms', []).append(name)
        # When several permitted entities carry the same paraphrase, keep that
        # ambiguity even if a vector ranks one of them much higher.
        corroborated = all_rewrites_resolved and len(matched_ids) == 1 and matched_ids <= leaders.keys()
        if corroborated:
            nid, = matched_ids
            ranked[nid]['evidence'] = 'paraphrase_and_vector_agree'
            ranked[nid]['corroborating_fields'] = leaders[nid]
            result['status'] = 'linked'
        elif len(matched_ids) > 1:
            result['status'] = 'ambiguous'
        elif vector_hits is None:
            result['status'] = 'unavailable'
        elif ranked:
            result['status'] = 'related'

        # Show exact corroboration candidates alongside their neighbors. A
        # paraphrase alone can be a suggestion when embeddings are unavailable.
        ordered = sorted(ranked.values(), key=lambda c: (
            c['node_id'] not in matched_ids, -(c['score'] or 0), c['node_id']))[:_CANDIDATES]
        result['candidates'] = ordered

    ids = {c['node_id'] for item in results for c in item['candidates']}
    labels = retriever.index.authorized_labels(ids, scopes=cohorts)
    for item in results:
        for candidate in item['candidates']:
            candidate['display_name'] = labels[candidate['node_id']]
            if candidate['score'] is not None:
                candidate['score'] = round(candidate['score'], 5)
    return results
