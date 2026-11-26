"""Score independent, version-pinned judgments against saved outputs, without another replay."""
from collections import Counter, defaultdict

from lightgraph.evaluation.fusion import pair_metrics, repeated_zero_positive_check


def _pair_state(mapping, left, right):
    a, b = mapping.get(left), mapping.get(right)
    if a is None or b is None:
        return 'unverified_output'
    return 'together' if a == b else 'separate'


def _verdict(expected, state):
    if expected == 'unknown':
        return 'unknown_truth'
    if state == 'unverified_output':
        return state
    if state == 'together':
        return 'tp' if expected == 'together' else 'fp'
    return 'fn' if expected == 'together' else 'tn'


def score(truth, run_id, artifact_id, node_ids, node_maps, edge_lines, input_maps=None, fact_map=None, relation_maps=None, blocks=()):
    if (truth.get('run_id'), truth.get('artifact_id')) != (run_id, artifact_id):
        raise ValueError('裁决清单的运行或产物与本次提交图不符。')
    rows = truth['pairs']
    evaluated = defaultdict(list)
    unique = set()
    memberships = defaultdict(set)
    for group in blocks:
        for nid in group['node_ids']:
            memberships[nid].add(group['group_id'])
    missed = []
    coverage = defaultdict(Counter)
    for row in rows:
        kind, left, right = (row[k] for k in ('kind', 'left', 'right'))
        expected, stratum = row['expected'], row['stratum']
        if kind == 'input_entity':
            def qualified(value):
                if not isinstance(value, list) or len(value) != 2 or not all(isinstance(k, str) and k for k in value):
                    raise ValueError('上游实体使用 [贡献单元, 原节点ID]。')
                return tuple(value)
            left, right = qualified(left), qualified(right)
        if (kind not in {'entity', 'canvas_line', 'input_entity', 'relation_fact'} or expected not in {'together', 'separate', 'unknown'}
                or not stratum or not row['evidence_ref'] or left == right):
            raise ValueError('裁决项缺少有效的对象对、预期、分层或依据。')
        if kind == 'input_entity':
            if not input_maps:
                raise ValueError('核算抽取节点裁决需要已重放的基线。')
            scenarios = input_maps
            ids = scenarios['committed']
        elif kind == 'entity':
            scenarios = node_maps
            ids = node_ids
        else:
            scenarios = {'committed_canvas': edge_lines} if kind == 'canvas_line' else {'declared_fact_model': fact_map or {}}
            for scenario, maps in (relation_maps or {}).items():
                scenarios[scenario] = maps[kind]
            ids = edge_lines
        pair = (kind, *sorted((left, right)))
        if pair in unique or left not in ids or right not in ids:
            raise ValueError('裁决项重复，或 ID 不属于指定版本。')
        unique.add(pair)
        if kind in {'entity', 'input_entity'} and expected == 'together':
            a, b = (scenarios['committed'].get(k) for k in (left, right))
            if None in (a, b):
                bucket = 'unverified_baseline_output'
            elif a == b:
                bucket = 'already_together_at_baseline'
            elif memberships[a] & memberships[b]:
                bucket = 'candidate_block_found'
            else:
                bucket = 'missed_by_candidate_blocks'
            coverage[kind, stratum][bucket] += 1
        for scenario, mapping in scenarios.items():
            verdict = _verdict(expected, _pair_state(mapping, left, right))
            evaluated[kind, scenario, stratum].append((expected, verdict))
            if verdict in {'fp', 'fn', 'unverified_output'}:
                missed.append({**row, 'scenario': scenario, 'result': verdict})
    table = []
    by_scenario = defaultdict(list)
    for (kind, scenario, stratum), outcomes in sorted(evaluated.items()):
        by_scenario[kind, scenario].extend(outcomes)
        counts = {k: sum(verdict == k for _, verdict in outcomes)
                  for k in ('tp', 'fp', 'fn', 'tn', 'unknown_truth', 'unverified_output')}
        tp, fp, fn = (counts[k] for k in ('tp', 'fp', 'fn'))
        table.append({'kind': kind, 'scenario': scenario, 'stratum': stratum, 'labeled_pairs': len(outcomes),
                      **counts, 'precision': tp/(tp+fp) if tp+fp else None,
                      'recall': tp/(tp+fn) if tp+fn else None, **pair_metrics(outcomes)})
    return {'basis': 'version_pinned_supplied_truth_pairs_only', 'pairs': len(rows), 'strata': table,
            'scenario_assessments': [{'kind': kind, 'scenario': scenario, **pair_metrics(outcomes)}
                                     for (kind, scenario), outcomes in sorted(by_scenario.items())],
            'disagreements_or_unverified': missed, 'positive_entity_candidate_coverage': [
                {'kind': k, 'stratum': label, **counts} for (k, label), counts in sorted(coverage.items())]}


def compare_input_pairs(truth, first, first_mapping, second_mapping):
    """Score source occurrences, so a split remains observable even if baseline IDs coincide."""
    if not truth['pairs']:
        raise ValueError('双提交评分至少需要一条独立的输入节点对标注。')
    if any(row.get('kind') != 'input_entity' for row in truth['pairs']):
        raise ValueError('双提交对照需要 input_entity 裁决，以原单元和节点 ID 标注两端。')
    result = score(truth, first['run_id'], first['artifact_id'], {}, {}, {},
                   input_maps={'committed': first_mapping, 'comparison': second_mapping})
    changed = {('fn', 'tp'): 'correct_merge', ('tn', 'fp'): 'new_false_merge',
               ('fp', 'tn'): 'correct_split', ('tp', 'fn'): 'new_false_split'}
    counts = Counter()
    pairs = []
    for row in truth['pairs']:
        left, right = tuple(row['left']), tuple(row['right'])
        before = _pair_state(first_mapping, left, right)
        after = _pair_state(second_mapping, left, right)
        if row['expected'] == 'unknown':
            change = 'unknown_truth'
        elif 'unverified_output' in (before, after):
            change = 'unverified_output'
        elif before == after:
            change = 'unchanged'
        else:
            change = changed[_verdict(row['expected'], before), _verdict(row['expected'], after)]
        counts[change] += 1
        pairs.append({**row, 'before': before, 'after': after, 'change': change})
    strata = [{**row, 'scenario': 'first' if row['scenario'] == 'committed' else 'second'}
              for row in result['strata']]
    return {'labeled_pairs': len(pairs), 'strata': strata,
            'scenario_assessments': [{**row, 'scenario': 'first' if row['scenario'] == 'committed' else 'second'}
                                     for row in result['scenario_assessments']],
            'satisfiability_review': repeated_zero_positive_check(pairs),
            'transition_counts': {key: counts[key] for key in (
                *changed.values(), 'unchanged', 'unknown_truth', 'unverified_output')}, 'pair_results': pairs}
