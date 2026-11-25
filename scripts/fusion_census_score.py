"""Score independent, version-pinned judgments against saved outputs, without another replay."""
from collections import Counter, defaultdict


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
            a, b = mapping.get(left), mapping.get(right)
            if expected == 'unknown':
                verdict = 'unknown_truth'
            elif a is None or b is None:
                verdict = 'unverified_output'
            elif a == b:
                verdict = 'tp' if expected == 'together' else 'fp'
            else:
                verdict = 'fn' if expected == 'together' else 'tn'
            evaluated[kind, scenario, stratum].append(verdict)
            if verdict in {'fp', 'fn', 'unverified_output'}:
                missed.append({**row, 'scenario': scenario, 'result': verdict})
    table = []
    for (kind, scenario, stratum), outcomes in sorted(evaluated.items()):
        counts = {k: outcomes.count(k) for k in ('tp', 'fp', 'fn', 'tn', 'unknown_truth', 'unverified_output')}
        tp, fp, fn = (counts[k] for k in ('tp', 'fp', 'fn'))
        table.append({'kind': kind, 'scenario': scenario, 'stratum': stratum, 'labeled_pairs': len(outcomes),
                      **counts, 'precision': tp/(tp+fp) if tp+fp else None,
                      'recall': tp/(tp+fn) if tp+fn else None})
    return {'basis': 'version_pinned_supplied_truth_pairs_only', 'pairs': len(rows), 'strata': table,
            'disagreements_or_unverified': missed, 'positive_entity_candidate_coverage': [
                {'kind': k, 'stratum': label, **counts} for (k, label), counts in sorted(coverage.items())]}
