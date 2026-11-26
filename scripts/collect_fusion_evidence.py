"""Join two saved census results, an archived review and the original unit observations, read-only."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path

from audit_checkpoint_merge import archived_state, direct_merge_parents
from audit_fusion_census import _verified_commit
from fusion_census_score import compare_input_pairs
from fusion_evidence_keys import observed_pair_checks
from fusion_evidence_members import collect_member_evidence
from fusion_evidence_routes import archived_entity_routes, match_pair_routes
from lightgraph.construction.checkpoints import load_checkpoint, read_checkpoint_overlap_review
from lightgraph.construction.projection_access import attach_checkpoint_grant
from lightgraph.construction.source_namespaces import project_checkpoint_inputs
from lightgraph.construction.workflow import run_output_path


def _dump(directory, name, data):
    with (directory / name).open('x', encoding='utf-8') as stream:
        json.dump(data, stream, sort_keys=True, ensure_ascii=False, allow_nan=False)
        stream.write('\n')


def _counts(counter):
    return dict(sorted(counter.items()))


def _review_stages(review):
    """Keep relation decisions alongside entity decisions, at their archived granularity."""
    decisions = {d['pattern_id']: d for d in review['decisions']}
    entity = {r['pattern_id']: r for r in review.get('proposal_checks', ())}
    relations = {r['pattern_id']: r for r in review.get('relation_proposal_checks', ())}
    cards = []
    for card in review['patterns']:
        pid = card['pattern_id']
        check = entity.get(pid) if card['kind'] == 'entity' else relations.get(pid)
        cards.append({'pattern_id': pid, 'kind': card['kind'], 'basis': card['basis'],
                      'eligible_actions': card['eligible_actions'],
                      'action': decisions.get(pid, {}).get('action'), 'archived_check': check})
    return {'summary': review['summary'], 'cards': cards,
            'analysis_omitted_patterns': review['summary'].get('analysis_omitted_patterns'),
            'note': 'Counts in archived_check apply to the entire pattern.'}


def _archived_inputs(run_file, expected, parents):
    run, project, state = archived_state(run_file)
    if run['run_id'] != expected['run_id']:
        raise ValueError('审查运行与第二份普查目录不符。')
    node_id = expected['node_id']
    target = state['artifacts'][node_id]
    if (target.get('artifact_status') != 'committed' or target['run_id'] != run['run_id']
            or target['artifact_id'] != expected['artifact_id'] or target['graph_digest'] != expected['graph_digest']
            or state['merge_nodes'][node_id]['merge_mode'] != 'overlap_review'):
        raise ValueError('请选择实际执行模型审查并提交该产物的运行。')

    def location(record):
        path = state.get('artifact_outputs', {}).get(record['node_id'], run['output_path'])
        return run_output_path(project, path)

    records = direct_merge_parents(state, target)
    if [{k: record[k] for k in ('node_id', 'artifact_id', 'graph_digest')} for record in records] != parents:
        raise ValueError('审查归档与普查目录的上游单元、产物或图摘要不一致。')
    review = read_checkpoint_overlap_review(location(target), target)
    if not review or review['upstream_artifacts'] != {r['node_id']: r['artifact_id'] for r in records}:
        raise ValueError('缺少可对账的已提交审查记录。')
    model = review['model_review']
    if model.get('status') != 'reviewed' or not model.get('model_calls'):
        raise ValueError('该次审查未取得完整有效裁决，请选择第三次有效审查运行。')
    contributors = [load_checkpoint(location(r), r) for r in records]
    inputs = [attach_checkpoint_grant(c, location(r), r) for c, r in
              zip(project_checkpoint_inputs(contributors, run['project_id']), records, strict=True)]
    return review, inputs, target['overlap_review_digest']


def collect(run_file, det_dir, review_dir, truth_file, output):
    if output.exists():
        raise FileExistsError(f'请使用新的结果目录：{output}')
    first, parents, first_map = _verified_commit(det_dir)
    second, other_parents, second_map = _verified_commit(review_dir)
    if (first['committed_merge_mode'] != 'deterministic' or second['committed_merge_mode'] != 'overlap_review'
            or first['run_id'] == second['run_id'] or first['artifact_id'] == second['artifact_id']
            or first['node_id'] != second['node_id'] or parents != other_parents or first_map.keys() != second_map.keys()):
        raise ValueError('需要同一批检查点、同一合并节点的确定性与模型审查两次独立提交。')
    truth_bytes = truth_file.read_bytes()
    truth = json.loads(truth_bytes.decode('utf-8-sig'))
    scored = compare_input_pairs(truth, first, first_map, second_map)
    review, inputs, review_digest = _archived_inputs(run_file, second, parents)
    routes = archived_entity_routes(review)
    requested = defaultdict(set)
    pairs = scored['pair_results']
    for pair in pairs:
        for owner, node_id in (pair['left'], pair['right']):
            requested[owner].add(node_id)
    members = {c['contributor_id']: collect_member_evidence(c, requested[c['contributor_id']])
               for c in inputs if c['contributor_id'] in requested}
    tasks = []
    attribution = defaultdict(Counter)
    lineage = defaultdict(Counter)
    for number, pair in enumerate(pairs):
        ends = []
        for side in ('left', 'right'):
            owner, node_id = pair[side]
            ends.append(members[owner][node_id])
        pair['pair_number'] = number + 1
        pair['output_ids'] = {name: [mapping[tuple(pair[s])] for s in ('left', 'right')]
                              for name, mapping in [('deterministic', first_map), ('model_review', second_map)]}
        pair['outputs_located_in_both_commits'] = all(
            node_id is not None for ids in pair['output_ids'].values() for node_id in ids)
        lineage[pair['stratum']]['located' if pair['outputs_located_in_both_commits'] else 'unverified'] += 1
        matches = match_pair_routes(routes, ends[0]['bindings'], ends[1]['bindings'])
        pair['route_matches'] = matches
        statuses = {m['status'] for m in matches}
        if any(e['status'] != 'available' or not e['bindings'] for e in ends):
            result = 'member_binding_unverified'
        elif 'selected_both_bindings' in statuses:
            result = 'both_physical_bindings_selected'
        elif matches:
            result = 'listed_pair_without_exact_selection'
        else:
            result = 'no_resolvable_pair_in_archived_entity_cards'
        pair['physical_route_result'] = result
        attribution[pair['stratum']][result] += 1
        for match in matches:
            if match['status'] == 'selected_both_bindings':
                tasks.append((pair['pair_number'], match['pattern_id'], tuple(pair['left']), tuple(pair['right'])))
    observations = observed_pair_checks(inputs, routes, tasks) if tasks else {}
    for number, pid, _, _ in tasks:
        pairs[number - 1].setdefault('local_key_observations_by_pattern', {})[pid] = observations[number, pid]
    summary = {'mode': 'archived_evidence_collection', 'collection_model_calls': 0,
               'deterministic': first, 'model_review': second, 'upstream_artifacts': parents,
               'archived_review_digest': review_digest, 'truth_sha256': hashlib.sha256(truth_bytes).hexdigest(),
               'archived_summary': review['summary'], 'archived_entity_cards': len(routes),
               'pairs': len(pairs), 'distinct_input_members': sum(map(len, requested.values())),
               'transition_counts': scored['transition_counts'], 'truth_scores_by_stratum': scored['strata'],
               'output_location_counts_by_stratum': {s: _counts(c) for s, c in sorted(lineage.items())},
               'pair_route_counts_by_stratum': {s: _counts(c) for s, c in sorted(attribution.items())},
               'matched_pair_pattern_checks': len(tasks)}
    output.mkdir(parents=True, exist_ok=False)
    _dump(output, 'review-stages.json', _review_stages(review))
    _dump(output, 'routes.json', routes)
    _dump(output, 'members.json', members)
    _dump(output, 'pairs.json', pairs)
    _dump(output, 'summary.json', summary)
    return summary


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    for flag in ('review-run', 'det-census', 'review-census', 'truth', 'output'):
        cli.add_argument('--' + flag, required=True, type=Path)
    args = cli.parse_args()
    summary = collect(args.review_run, args.det_census, args.review_census, args.truth, args.output)
    print(f"已核对 {summary['pairs']} 对标注，结果目录 {args.output}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
