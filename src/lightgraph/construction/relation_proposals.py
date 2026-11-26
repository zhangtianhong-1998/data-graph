"""Verify proposed relation facts on the merged graph and retain every contributing edge."""
from collections import Counter, defaultdict

from lightgraph.contracts import digest
from lightgraph.hierarchy_bindings import mapped_direction

from .relation_assertions import assertion_signature, support_address


def _members(edge, support):
    # A checkpoint-qualified original type survives specialization of an output
    # relation. Without it, use only a directly recorded, contributor-bound type.
    origins = defaultdict(set)
    for origin in support.get('endpoint_origins') or ():
        if origin.get('input_kind') == 'unit' and origin.get('contributor_id') and origin.get('type'):
            origins[origin['contributor_id']].add(origin['type'])
    members = set()
    for execution in support.get('execution_origins') or [support]:
        owner = execution.get('contributor_id') or support.get('contributor_id')
        declared = execution.get('definition') or (support.get('provenance') or {}).get('definition')
        if owner in origins:
            members.update((owner, kind) for kind in origins[owner])
        elif owner and declared == edge['type']:
            members.add((owner, declared))
        else:
            members.add((None, None))
    return members


def _bindings(support):
    prov = support.get('provenance') or {}
    return support_address(support), tuple(prov.get('source_key_columns') or ()), tuple(prov.get('target_key_columns') or ())


def _meaning(edge, definition, *, include_ordinary):
    orientation = definition.get('hierarchy_orientation')
    description = definition.get('description')
    if include_ordinary and orientation in (None, 'none'):
        family = definition.get('original_type') or definition.get('type')
        if not family or mapped_direction(family) or not isinstance(description, str) or not description.strip():
            return None
        # The member's original definition and the output must agree after
        # specialization of endpoint types. Compare all other business fields.
        signature = {key: value for key, value in definition.items() if key not in {
            'type', 'original_type', 'source_type', 'target_type', 'definition_variant', 'hierarchy_orientation'}}
        return 'directed', family, digest(signature)
    if orientation not in ('parent_to_child', 'child_to_parent'):
        return None
    # "Directed" alone says nothing about the business relationship. Distinct
    # types need the same explicit business description as well as an explicit
    # hierarchy declaration, qualified observations and already identical endpoints.
    if isinstance(description, str) and description.strip():
        return orientation, 'declared_description', description
    return orientation, 'relation_family', definition.get('original_type') or edge.get('original_type') or edge['type']


def annotate_relation_meanings(patterns, inputs):
    """Annotate review cards from frozen definitions, without reading nodes or disclosing their descriptions."""
    definitions = {(unit['contributor_id'], d['type']): d for unit in inputs
                   for d in unit['graph'].get('definitions', {}).get('relations', [])}
    for pattern in patterns:
        if pattern['kind'] != 'relation':
            continue
        pattern['relation_verification_version'] = 2
        meaning_groups = {}
        for member in pattern['members']:
            definition = definitions.get((member['contributor_id'], member['type']), {})
            member['hierarchy_orientation'] = definition.get('hierarchy_orientation')
            meaning = _meaning(member, definition, include_ordinary=True)
            member['meaning_group'] = meaning_groups.setdefault(meaning, len(meaning_groups)) if meaning else None
            if meaning:
                member['declared_meaning_digest'] = digest(meaning)
    return patterns


def _verify(pattern, selected, candidates, definitions, nodes):
    pid = pattern['pattern_id']
    source, = pattern['sources']
    requested = ((source['source_id'], source['schema'], source['table']),
                 tuple(source['source_key_columns']), tuple(source['target_key_columns']))
    by_pair = defaultdict(list)
    rejected = Counter()
    facts = []
    include_ordinary = pattern.get('relation_verification_version') == 2
    for edge, members in candidates:
        by_pair[edge['source'], edge['target']].append((edge, members))

    endpoints = 0
    by_member = {(m['contributor_id'], m['type']): m for m in pattern['members']}
    for (left, right), rows in sorted(by_pair.items()):
        owned_rows = []
        for edge, members in rows:
            if not members or not members <= selected:
                rejected['unverified_or_other_contributor'] += 1
            else:
                owned_rows.append((edge, members))
        rows = owned_rows
        if not rows:
            continue
        if len(rows) < 2 or len(set().union(*(members & selected for _, members in rows))) < 2:
            rejected['no_second_projection_at_directed_endpoints'] += len(rows)
            continue
        endpoints += 1
        verified = defaultdict(list)
        facets_by_meaning = defaultdict(lambda: defaultdict(set))
        for edge, members in rows:
            definition = definitions.get(edge['type']) or {}
            meaning = _meaning(edge, definition, include_ordinary=include_ordinary)
            reason = None
            if 'unverified_source_namespace' in pattern.get('reasons', ()):
                reason = 'unverified_source_namespace'
            elif 'unverified_access_fingerprint' in pattern.get('reasons', ()):
                reason = 'unverified_access_fingerprint'
            elif (left not in nodes or right not in nodes or not definition.get('source_type')
                  or nodes[left] != definition['source_type'] or nodes[right] != definition.get('target_type')):
                reason = 'unverified_endpoint_types'
            elif (not meaning or any(by_member[m].get('meaning_group') is None or
                  (meaning[0] != 'directed' and by_member[m].get('hierarchy_orientation') != meaning[0])
                  for m in members)):
                reason = 'unverified_declared_relation_meaning' if include_ordinary else 'no_declared_hierarchy_semantics'
            elif any(by_member[m].get('declared_meaning_digest') != digest(meaning) for m in members):
                reason = 'changed_original_relation_meaning'
            elif not all(_bindings(s) == requested for s in edge.get('supports') or ()):
                reason = 'different_directed_source_columns'
            else:
                reason, signature, facets = assertion_signature(edge, definition)
            if reason:
                rejected[reason] += 1
            else:
                verified[meaning, signature].append((edge['id'], members))
                for name, fingerprint in facets.items():
                    facets_by_meaning[meaning][name].add(fingerprint)
        meanings = {m for m, _ in verified}
        for (meaning, signature), group in sorted(verified.items()):
            owners = set().union(*(m for _, m in group))
            if len(group) < 2 or len(owners) < 2:
                if len(meanings) > 1:
                    reason = 'different_relation_meaning_or_orientation'
                elif len(verified) > 1:
                    fields = facets_by_meaning[meaning]
                    reason = next(('different_' + name for name in sorted(fields) if len(fields[name]) > 1),
                                  'different_rows_for_qualification')
                else:
                    reason = 'no_second_verified_projection'
                rejected[reason] += len(group)
                continue
            ids = sorted(eid for eid, _ in group)
            kind = 'direct_relationship' if meaning[0] == 'directed' else 'direct_hierarchy'
            facts.append({'fact_id': 'fact_' + digest(kind + '_v1', left, right, meaning, signature),
                          'fact_kind': kind,
                          'edge_ids': ids})
    return {'pattern_id': pid, 'selected_members': len(selected), 'examined_edges': len(candidates),
            'candidate_endpoint_groups': endpoints, 'rejected_by_reason': dict(sorted(rejected.items())),
            'groups': facts}


def verify_relation_proposals(merged, patterns, decisions):
    """Store edge IDs in the restricted review sidecar only; shared UI uses group counts."""
    patterns = {p['pattern_id']: p for p in patterns if p['kind'] == 'relation'}
    issued = {}
    routes, type_routes = defaultdict(set), defaultdict(set)
    for decision in decisions:
        if decision['action'] != 'propose_relation':
            continue
        pid = decision['pattern_id']
        p = patterns[pid]
        selected = {(p['members'][i]['contributor_id'], p['members'][i]['type']) for i in decision['member_indexes']}
        issued[pid] = selected
        for key in selected:
            routes[key].add(pid)
            type_routes[key[1]].add(pid)
    if not issued:
        return []
    definitions = {d['type']: d for d in merged['definitions']['relations']}
    nodes = {n['id']: n.get('type') for n in merged['nodes']}
    candidates = defaultdict(list)
    for edge in merged['edges']:
        members = set()
        for support in edge.get('supports') or ():
            members.update(_members(edge, support))
        routed = {pid for member in members for pid in routes.get(member, ())}
        if not members or (None, None) in members:
            routed.update(type_routes.get(edge['type'], ()))
        for pid in routed:
            candidates[pid].append((edge, members))
    reports = [_verify(patterns[pid], selected, candidates[pid], definitions, nodes)
               for pid, selected in sorted(issued.items())]
    claimed = Counter(eid for r in reports for group in r['groups'] for eid in group['edge_ids'])
    for report in reports:
        disjoint = []
        conflicts = 0
        for group in report['groups']:
            if any(claimed[eid] != 1 for eid in group['edge_ids']):
                conflicts += len(group['edge_ids'])
            else:
                disjoint.append(group)
        if conflicts:
            report['rejected_by_reason']['competing_relation_proposals'] = conflicts
        report.update(groups=disjoint, confirmed_fact_groups=len(disjoint),
                      grouped_output_edges=sum(len(g['edge_ids']) for g in disjoint),
                      status='verified' if disjoint else 'review')
    return reports
