"""Query-time intersection of typed dimension values within a witnessed source grain."""
from __future__ import annotations

from collections import defaultdict, deque
from itertools import product

from lightgraph.contracts import canonical
from lightgraph.source_values import scalar_kind
from .path_evidence import evidence_preview, path_node, grounds_slot
from .joint_evidence import common_record
from .retriever import _join_contexts, _observation_groups, _row_witnesses

_LIMIT = 4096
_PAGE = 96


def empty_answer(query, scopes, plan):
    return {'query': query, 'scopes': sorted(scopes), 'business_context': plan['business_context'], 'plan': plan,
            'slots': [{'surface': c['text'], 'role_hint': c['role'], 'candidates': c['candidates']}
                      for c in plan['conditions']], 'nodes': [], 'edges': [], 'bridge_nodes': [],
            'connected_components': [], 'truncated': False, 'truncation_reasons': []}


def _row_key(observation):
    rows = _row_witnesses([observation])
    if not rows:
        return None
    row = observation['row_context']
    # Include the typed bindings, even for a hand-written legacy row ID.
    return (*next(iter(rows.items())), canonical([row['values'], row.get('value_types', {})]))


def _values_match_identity(node, columns, values, types, witness):
    identity = node['identity']
    if not columns or len(values) != len(columns) or len(types) != len(columns):
        return False
    original = witness.get('source_identity')
    if original:
        return canonical(original['values']) == canonical(values) and original['value_types'] == types
    if witness.get('identity_decision_refs') or len(columns) != len(identity):
        return False
    names = witness.get('identity_key_names')
    if not names:
        if set(columns) == set(identity):
            names = columns
        elif len(columns) == 1:
            names = list(identity)
        else:
            return False
    if len(names) != len(identity) or set(names) != set(identity):
        return False
    return all(canonical(identity[name]) == canonical(value) and node['identity_types'].get(name) == value_type
               for name, value, value_type in zip(names, values, types))


def _bound_values(observation, columns):
    row = observation.get('row_context') or {}
    values = row.get('values') or {}
    declared = set((observation.get('provenance') or {}).get('context_columns') or ())
    if not columns or not set(columns) <= declared or not all(c in values and values[c] is not None for c in columns):
        return [], []
    types = row.get('value_types') or {}
    return [values[c] for c in columns], [types.get(c) or scalar_kind(values[c]) for c in columns]


def _binds_identity(node, observation, columns, witness):
    return _values_match_identity(node, columns, *_bound_values(observation, columns), witness)


def _lookup_binding(node, observation, end, columns, witness):
    for lookup in observation.get('lookup_matches') or ():
        if lookup.get('endpoint') != end or not columns or lookup.get('input_columns') != columns:
            continue
        values, types = _bound_values(observation, columns)
        if (not values or canonical(values) != canonical(lookup.get('input_values')) or types != lookup.get('input_value_types')
                or not _values_match_identity(node, lookup.get('key_columns'), lookup.get('resolved_key') or [],
                                              lookup.get('resolved_key_types') or [], witness)):
            continue
        if lookup.get('inherited_node_id') == node['id']:
            return True
        source = (witness.get('provenance') or {}).get('source')
        if not source or source != lookup.get('source') or (witness.get('provenance') or {}).get('key_columns') != lookup['key_columns']:
            continue
        # The lookup row records both the exact matched foreign-key values and
        # the dimension's typed business key. A same-spelling alias is insufficient.
        target = lookup.get('row_context') or {}
        matched = lookup.get('match_columns') or []
        resolved = lookup['key_columns']
        saved, saved_types = target.get('values') or {}, target.get('value_types') or {}
        if all(c in saved for c in [*matched, *resolved]) and (
                canonical([saved[c] for c in matched]) == canonical(values)
                and [saved_types.get(c) or scalar_kind(saved[c]) for c in matched] == types
                and canonical([saved[c] for c in resolved]) == canonical(lookup['resolved_key'])
                and [saved_types.get(c) or scalar_kind(saved[c]) for c in resolved] == lookup['resolved_key_types']):
            return True
    return False


def _endpoint(node, observation, edge, row_key, context, slot=None):
    end = 'source' if edge['source'] == node['id'] else 'target'
    columns = (observation.get('provenance') or {}).get(end + '_key_columns')
    lookup_required = any(m.get('endpoint') == end for m in observation.get('lookup_matches') or ())
    for node_context, group in _observation_groups(node):
        joined = _join_contexts(context, node_context)
        if joined is None or joined['status'] == 'unknown':
            continue
        for witness in group:
            bindings = (observation.get('identity_decision_endpoints') or {}).get(end, {})
            members = witness.get('identity_decision_members') or {}
            if any(members.get(version) != member for version, member in bindings.items()):
                continue
            if slot and not grounds_slot(node, witness, slot):
                continue
            key = _row_key(witness)
            if key and key[0] == row_key[0] and key != row_key:
                continue
            if (_binds_identity(node, observation, columns, witness) if not lookup_required
                    else _lookup_binding(node, observation, end, columns, witness)):
                yield joined, witness


def _center_witnesses(node, context, row_key):
    for node_context, group in _observation_groups(node):
        joined = _join_contexts(context, node_context)
        if joined is None or joined['status'] == 'unknown':
            continue
        for witness in group:
            key = _row_key(witness)
            if not key or key[0] != row_key[0] or key == row_key:
                yield joined, witness


def retrieve_dimensional(retriever, query, scopes, plan, *, direction='outgoing', relation_types=(),
                         max_depth=12, max_nodes=80, max_edges=150):
    """A common graph neighbor is only a candidate. Verify all bound values in the same source tuple."""
    answer = empty_answer(query, scopes, plan)
    slots = plan['conditions']
    details = {'status': 'no_match', 'matched_conditions': [], 'fact_ids': [],
               'children': [], 'witnesses': [], 'stop_reason': 'unresolved_condition'}
    answer['dimensional'] = details
    if len(slots) < 2 or any(not s['candidates'] for s in slots):
        return answer

    selection = plan['business_context']
    cohorts = retriever.authorized_cohorts(scopes, selection)
    hierarchies = retriever.index.authorized_hierarchies(cohorts) if plan.get('drill_role') and not relation_types else None
    reasons = {'candidate_limit'} if plan['candidate_limit'] else set()
    nodes, node_output, edge_output = {}, {}, {}
    facts = defaultdict(lambda: defaultdict(list))
    read_count = 0
    work = 0
    cache = {}

    def neighbors(nid, traversal='both', relations=(), allowed=None, hierarchical=False):
        nonlocal read_count
        key = (nid, traversal, tuple(relations), tuple(sorted(allowed)) if allowed is not None else None, hierarchical)
        if key not in cache:
            if read_count >= 100:
                reasons.add('adjacency_budget_reached')
                return []
            page = retriever.index.expand_neighborhood([nid], scopes=cohorts, hops=1,
                direction=traversal, relation_types=relations, allowed_neighbor_ids=allowed,
                hierarchies=hierarchies if hierarchical else None,
                max_nodes=_PAGE + 1, max_edges=_PAGE, max_per_rel=_PAGE)
            read_count += 1
            nodes.update(page['nodes'])
            cache[key] = page['edges']
            reasons.update(page['truncation_reasons'])
        return cache[key]

    def within_budget():
        nonlocal work
        work += 1
        if work > _LIMIT:
            reasons.add('witness_budget_reached')
            return False
        return True

    # Start with the most selective dimension under the caller's grant. The
    # partial intersection restricts subsequent high-degree probes inside SQL.
    candidates = {c['node_id'] for item in slots for c in item['candidates']}
    anchors = retriever.index.load_authorized_nodes(candidates, scopes=cohorts)
    order = sorted(range(len(slots)), key=lambda i: sum(anchors[c['node_id']]['degree'] for c in slots[i]['candidates']))
    viable = None
    for slot in order:
        item = slots[slot]
        centers = {key[0] for key in viable} if viable is not None else None
        for candidate in item['candidates']:
            nid = candidate['node_id']
            allowed = centers
            if centers is not None and nid in centers:
                allowed = centers | candidates  # This condition can be the middle of a chain.
            for edge in neighbors(nid, allowed=allowed):
                center = edge['target'] if edge['source'] == nid else edge['source']
                if center == nid:
                    continue
                for edge_ctx, group in _observation_groups(edge):
                    for obs in group:
                        if not within_budget():
                            break
                        grain = _row_key(obs)
                        if not grain:
                            continue
                        for ctx, witness in _endpoint(nodes[nid], obs, edge, grain, edge_ctx, item):
                            binding = (nid, edge, obs, ctx, witness)
                            if viable is None or (center, grain) in viable:
                                facts[center, grain][slot].append(binding)
                            # The middle point of a directed chain can itself
                            # fulfill a condition, e.g. metric → region → product.
                            if viable is None or (nid, grain) in viable:
                                facts[nid, grain][slot].append(binding)
                            if not within_budget():
                                break
                    if work > _LIMIT:
                        break
                if work > _LIMIT:
                    break
            if work > _LIMIT:
                break
        viable = {key for key, bindings in facts.items() if slot in bindings and (viable is None or key in viable)}
        if not viable or work > _LIMIT:
            break

    def emit_node(nid, witness, matches=()):
        if nid not in node_output:
            node_output[nid] = path_node(nodes[nid], evidence_preview(witness), list(matches), bool(matches))
        else:
            row = node_output[nid]
            row['matched_slots'] = sorted(set(row['matched_slots']).union(matches))
            row['is_seed'] = bool(row['matched_slots'])
            preview = evidence_preview(witness)
            if preview not in row['observations']:
                row['observations'].append(preview)

    def emit_edge(edge, observation):
        preview = evidence_preview(observation)
        if edge['id'] not in edge_output:
            edge_output[edge['id']] = {**{k: edge[k] for k in ('id', 'source', 'target', 'relation')},
                                       'observations': [], 'evidence_refs': [], 'source_tables': []}
        saved = edge_output[edge['id']]
        if preview not in saved['observations']:
            saved['observations'].append(preview)
            for key in ('evidence_refs', 'source_tables'):
                saved[key] = sorted(set(saved[key]).union(preview.get(key, [])))

    # Verify the complete conjunction before using its graph edges as an answer.
    qualified = []
    proofs = set()
    unverified_row = False
    for (center, grain), hits in sorted(facts.items()):
        if len(hits) != len(slots):
            continue
        for combo in product(*(hits[i] for i in range(len(slots)))):
            if not within_budget():
                break
            if len({part[0] for part in combo}) != len(slots):
                continue
            used_edges = {part[1]['id'] for part in combo if part[0] != center}
            if any(nid == center and edge['id'] not in used_edges for nid, edge, *_ in combo):
                continue
            ctx = combo[0][3]
            for part in combo[1:]:
                ctx = _join_contexts(ctx, part[3])
                if ctx is None:
                    break
            if ctx is None:
                continue
            row_observations = [part[2] for part in combo]
            row_proof = common_record(row_observations)
            if not row_proof:
                unverified_row = True
                continue
            central = next(_center_witnesses(nodes[center], ctx, grain), None)
            if not central:
                continue
            ctx, center_obs = central
            needed = {center, *(part[0] for part in combo)}
            edge_ids = sorted(used_edges)
            ids = tuple(part[0] for part in combo)
            signature = (center, grain, ids, canonical(ctx))
            if signature in proofs:
                continue
            if (len(node_output.keys() | needed) > max_nodes or len(edge_output.keys() | set(edge_ids)) > max_edges
                    or len(qualified) >= 32):
                reasons.add('output_limit')
                break
            emit_node(center, center_obs)
            for slot, (nid, edge, obs, _, witness) in zip(slots, combo):
                emit_node(nid, witness, [slot['text']])
                emit_edge(edge, obs)
            proofs.add(signature)
            details['witnesses'].append({'fact_id': center, 'row_id': grain[1],
                'source': combo[0][2]['provenance']['source'], 'grain_columns': combo[0][2]['provenance']['context_columns'],
                'edge_ids': edge_ids, 'conditions': [{'text': s['text'], 'node_id': nid} for s, nid in zip(slots, ids)],
                'business_context': ctx, 'common_record': row_proof})
            qualified.append((center, grain, ctx, ids, row_observations))
    children, level_hits = set(), set()
    if qualified and plan.get('drill_role'):
        for center, grain, context, ids, row_observations in qualified:
            # A global/catalog hierarchy may be stored separately. Its child
            # must also be bound independently to this very fact tuple.
            memberships = defaultdict(list)
            for membership in neighbors(center):
                child_id = membership['target'] if membership['source'] == center else membership['source']
                for member_ctx, member_obs in _observation_groups(membership):
                    for obs in member_obs:
                        if not within_budget():
                            break
                        if _row_key(obs) == grain:
                            memberships[child_id].append((membership, obs, member_ctx))
            queue = deque((nid, 0, context, row_observations) for nid, s in zip(ids, slots) if s['role'] == plan['drill_role'])
            seen = set()
            while queue and within_budget():
                parent, depth, ctx, ancestor_observations = queue.popleft()
                signature = (parent, canonical(ctx))
                if signature in seen:
                    continue
                seen.add(signature)
                for edge in neighbors(parent, direction if relation_types else 'both', relation_types, hierarchical=True):
                    child = edge['target'] if edge['source'] == parent else edge['source']
                    if child in ids or child == parent:
                        continue
                    for edge_ctx, observations in _observation_groups(edge):
                        joined = _join_contexts(ctx, edge_ctx)
                        if joined is None:
                            continue
                        for obs in observations:
                            if not within_budget() or joined['status'] == 'unknown':
                                continue
                            hierarchy_grain = _row_key(obs)
                            membership_proof = None
                            if hierarchy_grain == grain:
                                if not common_record([*ancestor_observations, obs]):
                                    continue
                                match = next(_endpoint(nodes[child], obs, edge, grain, joined), None)
                            else:
                                if hierarchy_grain and hierarchy_grain[0] == grain[0]:
                                    continue
                                match = None
                                for member_edge, member_obs, member_ctx in memberships.get(child, ()):
                                    verified = _join_contexts(joined, member_ctx)
                                    if verified and common_record([*ancestor_observations, member_obs]):
                                        match = next(_endpoint(nodes[child], member_obs, member_edge, grain, verified), None)
                                    if match:
                                        membership_proof = (member_edge, member_obs)
                                        break
                            if not match or (child, canonical(match[0])) in seen:
                                continue
                            additions = {edge['id']}
                            if membership_proof:
                                additions.add(membership_proof[0]['id'])
                            if (depth >= max_depth or child not in node_output and len(node_output) >= max_nodes
                                    or len(edge_output.keys() | additions) > max_edges):
                                reasons.add('drilldown_limit')
                                continue
                            emit_node(child, match[1])
                            emit_edge(edge, obs)
                            if membership_proof:
                                emit_edge(*membership_proof)
                            children.add(child)
                            if plan.get('requested_level') and nodes[child]['kind'] == plan['requested_level']:
                                level_hits.add(child)
                            else:
                                verified_obs = membership_proof[1] if membership_proof else obs
                                queue.append((child, depth + 1, match[0], [*ancestor_observations, verified_obs]))
                            break
    if qualified:
        details.update(status='matched', matched_conditions=[s['text'] for s in slots],
                       fact_ids=sorted({w['fact_id'] for w in details['witnesses']}), children=sorted(children),
                       level_node_ids=sorted(level_hits),
                       multiple_bindings=len({ids for _, _, _, ids, _ in qualified}) > 1,
                       stop_reason=('search_limit' if reasons else 'level_reached' if level_hits else
                                    'requested_level_unreached' if plan.get('requested_level') else
                                    'no_further_witnessed_child' if plan['drill_role'] else 'conditions_met'))
    else:
        details.update(status='incomplete' if reasons else 'no_common_fact',
                       stop_reason='search_limit' if reasons else 'row_cooccurrence_unverified' if unverified_row else 'no_joint_row_evidence')
    answer.update(nodes=list(node_output.values()), edges=list(edge_output.values()),
                  truncated=bool(reasons), truncation_reasons=sorted(reasons))
    return answer
