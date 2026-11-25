"""Measure a union of checkpoint-bound hypotheses with the production merger, in memory."""
from collections import Counter, defaultdict
from dataclasses import dataclass
import json
from pathlib import Path

from lightgraph.construction import merger
from lightgraph.contracts import canonical, digest
from lightgraph.runtime.config import MergePolicyConfig, ReviewedKeyPairRule

if __package__:
    from . import audit_checkpoint_merge as audit
    from .fusion_census_edges import analyze_relation_facts, canvas_counts
else:
    import audit_checkpoint_merge as audit
    from fusion_census_edges import analyze_relation_facts, canvas_counts


def _project_nodes(original, current):
    destinations = defaultdict(set)
    for output, members in current.items():
        for member in members:
            destinations[member].add(output)
    mappings = {}
    unresolved = {}
    for old, members in original.items():
        if not members or any(member not in destinations for member in members):
            mappings[old] = None
            unresolved[old] = 'missing_member'
            continue
        outputs = set().union(*(destinations[m] for m in members))
        mappings[old] = next(iter(outputs)) if len(outputs) == 1 else None
        if len(outputs) != 1:
            unresolved[old] = 'split_or_ambiguous_member'
    return mappings, unresolved


def _rule_union(run, node_id, target, parents, policy, simulations):
    """Reject competing uses of the same side, including overlap with an existing rule."""
    additions = []
    for source in simulations:
        doc = source if isinstance(source, dict) else json.loads(Path(source).read_text(encoding='utf-8-sig'))
        if doc.get('mode') != 'simulation_only':
            raise ValueError('联合实验只接受带 simulation_only 标记的模拟规则。')
        reviewed = audit._simulated_policy(doc, run, node_id, target, parents, {})
        rule, = reviewed['reviewed_key_pairs']
        if rule['approval_ref'] != 'simulation_only':
            raise ValueError('模拟规则的来源标记不一致。')
        additions.append(rule)
    existing = policy.get('reviewed_key_pairs', [])
    owners = defaultdict(list)
    for source, rules in (('existing', existing), ('simulation', sorted(additions, key=canonical))):
        for rule in rules:
            ref = (source, digest(rule))
            for p in rule['projections']:
                for token in rule['observation_digests']:
                    # A different opposite unit or name column does not give an
                    # observation another owner. Distinct physical axes stay separate.
                    key = (rule['entity_type'], p['contributor_id'], p['source_id'], p['schema'],
                           p['table'], p['key_column'], token)
                    owners[key].append(ref)
    conflicts = [{'physical_claim': k, 'rules': owners[k]} for k in sorted(owners)
                 if len(owners[k]) > 1 and any(source == 'simulation' for source, _ in owners[k])]
    joined = {**policy, 'reviewed_key_pairs': [*existing, *sorted(additions, key=canonical)]}
    return joined, conflicts


@dataclass
class _EdgeLineage:
    by_input: dict
    uncertain_outputs: set
    unlocated_inputs: int = 0

    def capture(self, owner, raw, output, possible):
        self.by_input[owner, raw] = output
        if output is None:
            self.uncertain_outputs.update(possible)
            if not possible:
                self.unlocated_inputs += 1


def _project_edges(edge_ids, original, current):
    """An old edge projects only if *all* of its qualified raw inputs reach one unambiguous new edge."""
    targets = defaultdict(set)
    for raw, old in original.by_input.items():
        if old is not None:
            targets[old].add(current.by_input.get(raw))
    result, gaps = {}, Counter()
    for old in edge_ids:
        options = targets[old]
        # With no location at all for an old input we cannot establish which old
        # output had a complete set of members. Never substitute an equal-looking ID.
        if original.unlocated_inputs:
            reason = 'unlocated_baseline_input'
        elif old in original.uncertain_outputs:
            reason = 'ambiguous_baseline_input'
        elif not options:
            reason = 'no_verified_baseline_member'
        elif None in options:
            reason = 'missing_or_ambiguous_simulated_member'
        elif len(options) > 1:
            reason = 'baseline_edge_split'
        elif options & current.uncertain_outputs:
            reason = 'ambiguous_simulated_output'
        else:
            result[old] = next(iter(options))
            continue
        result[old] = None
        gaps[reason] += 1
    return result, dict(gaps)


@dataclass
class _Snapshot:
    report: dict
    members: dict
    edge_identity: dict
    edge_members: _EdgeLineage
    canvas_line: dict
    relation_fact: dict


def _replay(inputs, policy, target, relation_families=()):
    stage = set()
    inputs_by_owner = defaultdict(dict)
    input_occurrences = Counter()
    members = defaultdict(set)

    def capture_node(kind, owner, original_id, output_id):
        if kind != 'unit':
            raise ValueError('逐节点映射需要直接上游抽取单元。')
        key = (owner, original_id)
        input_occurrences[key] += 1
        inputs_by_owner[owner][original_id] = output_id if input_occurrences[key] == 1 else None
        if output_id is not None:
            members[output_id].add(key)

    def capture(refs):
        stage.update(audit._edge_key(ref, 'edge_id') for ref in refs)

    graph = merger.merge_construction_graphs(inputs, target_access_fingerprint=target['access_fingerprint'],
                                            merge_policy=policy, recognize_source_replays=True,
                                            observe_before_hierarchy=capture, observe_node_lineage=capture_node)
    expected = Counter((i['contributor_id'], n['id']) for i in inputs for n in i['graph']['nodes'])
    if expected != input_occurrences:
        raise ValueError('原始节点映射的贡献者、ID 或出现次数与检查点不一致。')
    duplicate_keys = {key for key, count in input_occurrences.items() if count > 1}
    verified = sum(n is not None for local in inputs_by_owner.values() for n in local.values())
    mapping_counts = {'input_occurrences': input_occurrences.total(), 'unique_qualified_ids': len(input_occurrences),
                      'verified': verified, 'unverified': len(input_occurrences) - verified,
                      'unverified_occurrences': input_occurrences.total() - verified,
                      'duplicate_qualified_ids': len(duplicate_keys)}
    edge_members = _EdgeLineage({}, set())
    lineage = audit.edge_lineage(inputs, graph, stage, observe_input_edge=edge_members.capture)
    reasons = Counter()
    for counts in lineage['input_edge_counts_by_contributor'].values():
        reasons.update(counts)
    traced_edge_reasons = {'unchanged', 'qualified_origin', 'stage_tuple_and_owner_match_checkpoint',
                           'legacy_tuple_and_owner_match_checkpoint'}
    membership = audit._simulated_membership(graph)
    assertions = analyze_relation_facts(graph, list(relation_families))
    canvas_line = {}
    report = {
        'relation_facts': assertions['summary'], 'confirmed_relation_groups': assertions['confirmed_groups'],
        'nodes': len(graph['nodes']), **canvas_counts(graph, observe_line=canvas_line.__setitem__),
        'integrity': audit.output_checks(graph), 'edge_lineage': lineage,
        'unverified_input_edge_occurrences': sum(n for status, n in reasons.items() if status not in traced_edge_reasons),
        'unverified_origin_bindings': sum(n for items in [lineage['qualified_origin_bindings'],
            lineage['before_hierarchy_replay']['origin_bindings']] for status, n in items.items() if status != 'verified'),
        'row_digests': audit._graph_row_digests(graph),
        'reviewed_key_pairs': graph['fusion_diagnostics'].get('reviewed_key_pairs', {}),
        'reviewed_membership': membership,
        'input_node_mapping': dict(inputs_by_owner), 'input_node_mapping_counts': mapping_counts,
    }
    rule_winners = {nid: [] for nid in membership['groups_by_output_id']}
    if rule_winners:
        for node in graph['nodes']:
            if node['id'] in rule_winners:
                rule_winners[node['id']] = sorted({ref for support in node.get('supports', [])
                    if support.get('identity_basis') == 'reviewed_key_name_v1'
                    for ref in support.get('identity_decision_members', {})})
    report['reviewed_group_rule_refs'] = dict(rule_winners)
    # If two source rows use the same qualified ID, a labelled pair referring
    # to that ID cannot identify a specific occurrence.
    qualified = {n['id']: members[n['id']] if members[n['id']].isdisjoint(duplicate_keys) else set()
                 for n in graph['nodes']}
    return _Snapshot(report, qualified, {e['id']: (e['type'], e['source'], e['target']) for e in graph['edges']},
                     edge_members, canvas_line, assertions['edge_to_fact'])


@dataclass
class _Baseline:
    binding: tuple
    snapshot: _Snapshot
    rows_match_committed: dict


def _binding(target, policy, relation_families):
    return (target['artifact_id'], target['graph_digest'], target['access_fingerprint'],
            tuple(target['upstream_artifacts']), digest(policy), digest(relation_families))


def prepare_replay_baseline(*, inputs, policy, target, committed=None, committed_row_digests=None, relation_families=()):
    """Replay the recorded policy once; keep small indexes for comparing independent scenarios."""
    if (committed is None) == (committed_row_digests is None):
        raise ValueError('需要一份提交图或其完整节点和边行摘要。')
    original = audit._graph_row_digests(committed) if committed is not None else committed_row_digests
    if set(original) != {'nodes', 'edges'}:
        raise ValueError('提交图的行摘要不完整。')
    snapshot = _replay(inputs, policy, target, relation_families)
    matches = {k: original[k] == snapshot.report['row_digests'][k] for k in original}
    return _Baseline(_binding(target, policy, relation_families), snapshot, matches)


def run_joint_replay(*, run, node_id, target, parents, policy, inputs, committed, simulation_paths, baseline=None,
                     relation_families=()):
    """Return measured effects of applying the finite rules simultaneously. Never writes project state."""
    result = {'mode': 'simulation_only', 'model_calls': 0, 'run_id': run['run_id'], 'node_id': node_id,
              'artifact_id': target['artifact_id'], 'upstream_artifacts': target['upstream_artifacts'],
              'simulation_files': len(simulation_paths)}
    if not simulation_paths:
        return {**result, 'status': 'not_run_no_simulations'}
    if ([p['artifact_id'] for p in parents] != target['upstream_artifacts'] or
            [item['contributor_id'] for item in inputs] != [p['node_id'] for p in parents]):
        raise ValueError('联合重放输入单元与合并检查点不一致。')
    joined, conflicts = _rule_union(run, node_id, target, parents, policy, simulation_paths)
    result['rule_count_including_existing'] = len(joined['reviewed_key_pairs'])
    if conflicts:
        return {**result, 'status': 'blocked_conflicting_claims', 'conflicts': conflicts}
    max_rules = MergePolicyConfig.model_json_schema()['properties']['reviewed_key_pairs']['maxItems']
    if result['rule_count_including_existing'] > max_rules:
        return {**result, 'status': 'blocked_policy_rule_limit', 'max_rules': max_rules}
    MergePolicyConfig.model_validate(joined)
    result['simulated_policy_digest'] = digest(joined)
    if baseline is None:
        baseline = prepare_replay_baseline(inputs=inputs, policy=policy, target=target, committed=committed,
                                           relation_families=relation_families)
    elif baseline.binding != _binding(target, policy, relation_families):
        raise ValueError('联合重放的基线对应其他检查点或策略。')
    original = baseline.snapshot
    result.update(baseline=original.report, rows_match_committed=baseline.rows_match_committed)
    if not all(baseline.rows_match_committed.values()):
        return {**result, 'status': 'baseline_mismatch'}
    current = _replay(inputs, joined, target, relation_families)
    node_map, unresolved = _project_nodes(original.members, current.members)
    # ID replacements and changes under a retained ID are different from deleted source records.
    before, after = original.edge_identity, current.edge_identity
    edge_map, edge_gaps = _project_edges(before, original.edge_members, current.edge_members)
    relation_scoring = {'canvas_line': {old: current.canvas_line.get(new) for old, new in edge_map.items()},
                        'relation_fact': {old: current.relation_fact.get(new) for old, new in edge_map.items()}}
    common = before.keys() & after.keys()
    delta = {
        'nodes_removed': original.report['nodes'] - current.report['nodes'],
        'raw_edges_removed': original.report['raw_edges'] - current.report['raw_edges'],
        'distinct_assertions_removed': (original.report['relation_facts']['hypothetical_distinct_assertions']
                                       - current.report['relation_facts']['hypothetical_distinct_assertions']),
        'canvas_lines_removed': original.report['default_canvas_lines'] - current.report['default_canvas_lines'],
        'hierarchy_self_loops_removed': original.report['hierarchy_self_loops'] - current.report['hierarchy_self_loops'],
        'common_edge_ids': len(common),
        'common_edge_ids_changed_type_or_endpoints': sum(before[key] != after[key] for key in common),
        'previous_only_edge_ids': len(before.keys() - after.keys()),
        'simulation_only_edge_ids': len(after.keys() - before.keys()),
    }
    incoming = Counter(n for n in node_map.values() if n is not None)
    by_rule = []
    refs = current.report['reviewed_group_rule_refs']
    for cfg in joined['reviewed_key_pairs']:
        rule = ReviewedKeyPairRule.model_validate(cfg).model_dump(mode='json', by_alias=True)
        rid = 'reviewed_keys_' + digest(rule)
        winners = [n for n, decision_ids in refs.items() if decision_ids == [rid]]
        by_rule.append({'rule_id': rid, 'entity_type': rule['entity_type'], 'projections': rule['projections'],
                        'requested_observations': len(rule['observation_digests']), 'output_groups': len(winners),
                        'explained_net_nodes': sum(max(incoming[n]-1, 0) for n in winners)})
    delta['net_nodes_outside_attributed_groups'] = delta['nodes_removed'] - sum(r['explained_net_nodes'] for r in by_rule)
    unverified = (bool(unresolved or edge_gaps) or any(
        snap.report['unverified_input_edge_occurrences'] or snap.report['unverified_origin_bindings']
        or snap.report['input_node_mapping_counts']['unverified']
        or any(n for key, n in snap.report['integrity'].items() if key != 'self_links')
        for snap in (original, current)))
    return {**result, 'status': 'completed_with_unverified' if unverified else 'completed',
            'simulated': current.report, 'delta': delta, 'rule_attribution': by_rule, 'node_mapping': node_map,
            'relation_scoring': relation_scoring, 'unverified_edge_mapping_counts': edge_gaps,
            'unverified_node_mappings': unresolved}
