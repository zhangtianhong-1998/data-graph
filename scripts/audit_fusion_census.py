"""Run a read-only full-graph disambiguation experiment against one archived merge."""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import sys
import time

from lightgraph.construction.checkpoints import load_checkpoint
from lightgraph.construction.workflow import run_output_path
from lightgraph.contracts import canonical

from audit_checkpoint_merge import (
    _graph_row_digests, _simulated_policy, archived_state, committed_merge_policy, direct_merge_parents,
)
from fusion_census_edges import analyze_edges, analyze_relation_facts
from fusion_census_hypotheses import discover, summarize_multiway_outputs
from fusion_census_nodes import analyze_nodes
from fusion_census_replay import prepare_replay_baseline, run_joint_replay
from fusion_census_score import compare_input_pairs, score


def read(path):
    return Path(path).read_text(encoding='utf-8-sig')


def write(directory, filename, obj):
    with (directory / filename).open('x', encoding='utf-8') as stream:
        json.dump(obj, stream, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        stream.write('\n')


def census_stage(location, record, directory, unit, excluded_values, families):
    start = time.monotonic()
    checkpoint = load_checkpoint(location, record)
    graph = checkpoint['graph']
    directory.mkdir()
    if not unit:
        # The worker already verified the archive. Only these fields are needed to replay later.
        write(directory, 'replay-input.json', {'row_digests': _graph_row_digests(graph),
              'graph': {'fusion_policy': graph.get('fusion_policy')}, 'overlap_review': checkpoint.get('overlap_review')})
    nodes = analyze_nodes(graph, contributor_id=record['node_id'] if unit else None, excluded_values=excluded_values)
    write(directory, 'node-ids.json', sorted(n['id'] for n in graph['nodes']))
    node_counts = nodes['summary']
    physical = {k: nodes['physical_hypothesis'][k]['hypothetical_node_reduction']
                for k in ('within_only', 'cross_only', 'joint')}
    write(directory, 'nodes.json', nodes)
    del nodes
    edges = analyze_edges(graph)
    edge_counts = edges['summary']
    write(directory, 'edges.json', edges)
    del edges
    facts = analyze_relation_facts(graph, families)
    write(directory, 'relation-facts.json', facts)
    print(f"完成 {record['node_id']} 全图普查", flush=True)
    return {'node_id': record['node_id'], 'artifact_id': record['artifact_id'], 'graph_digest': record['graph_digest'],
            'checkpoint_run_id': record['run_id'], 'artifact_status': record['artifact_status'],
            'kind': 'unit' if unit else 'committed', 'directory': directory.name,
            'rows': {'nodes': len(graph['nodes']), 'edges': len(graph['edges'])},
            'nodes': node_counts, 'edges': edge_counts, 'relation_facts': facts['summary'],
            'physical_hypothesis': physical, 'elapsed_seconds': round(time.monotonic()-start, 2)}


def run_stages(tasks, workers):
    results = {}
    errors = {}
    if workers == 1:
        for args in tasks:
            key = args[1]['node_id']
            try:
                results[key] = census_stage(*args)
            except Exception as exc:
                errors[key] = f'{type(exc).__name__}: {exc}'
    else:
        with ProcessPoolExecutor(max_workers=min(workers, len(tasks))) as pool:
            pending = {pool.submit(census_stage, *args): args[1]['node_id'] for args in tasks}
            for future in as_completed(pending):
                key = pending[future]
                try:
                    results[key] = future.result()
                except Exception as exc:
                    errors[key] = f'{type(exc).__name__}: {exc}'
    return results, errors


def _report(out, report):
    stages = report['stages']
    lines = ['# 全图节点与关系消歧实验', '', f"运行 `{report['run_id']}`，合并产物 `{report['artifact_id']}`。",
             '', f"这次离线普查调用模型 {report['model_calls']} 次。归档合并模式为 `{report['committed_merge_mode']}`。"]
    historical = report.get('historical_pattern_review', {}).get('summary')
    if historical:
        lines.append(f"归档合并调用模型 {historical.get('model_calls', '未记录')} 次，"
                     f"完成审查 {historical.get('reviewed_patterns', '未记录')} 个模式，"
                     f"新增生效实体规则 {historical.get('added_entity_rules', '未记录')} 条。")
    lines += ['', '|检查点|节点|原始边|涉及候选的节点|分组后的画布连线|', '|---|---:|---:|---:|---:|']
    for key, stage in stages.items():
        lines.append(f"|{key}|{stage['rows']['nodes']}|{stage['rows']['edges']}|"
                     f"{stage['nodes']['candidate_nodes']}|{stage['edges']['default_canvas_lines']}|")
    lines.extend(['', '|检查点|同单元草稿假设净减节点|跨单元假设净减节点|同物理键联合假设净减节点|'
                  '同义关系族中可归一的断言差额|仍缺证或有分歧的端点组|',
                  '|---|---:|---:|---:|---:|---:|'])
    for key, stage in stages.items():
        physical = stage['physical_hypothesis']
        facts = stage['relation_facts']
        lines.append(f"|{key}|{physical['within_only']}|{physical['cross_only']}|{physical['joint']}|"
                     f"{facts['confirmed_reduction']}|{facts['candidate_groups_with_unresolved_pairs']}|")
    if not report.get('committed_only'):
        lines.extend(['', '|实验|运行结果|净减节点|净减原始边|净减画布线|净减断言（按本次假设）|', '|---|---|---:|---:|---:|---:|'])
    for label, scenario in report.get('scenarios', {}).items():
        delta = scenario.get('delta', {})
        lines.append(f"|{label}|{scenario['status']}|{delta.get('nodes_removed', '—')}|"
                     f"{delta.get('raw_edges_removed', '—')}|{delta.get('canvas_lines_removed', '—')}|"
                     f"{delta.get('distinct_assertions_removed', '—')}|")
    if 'baseline_checks' in report:
        committed = stages[report['node_id']]
        base = committed['rows']
        lines.extend(['', '|图|实体节点|原始边|条件一致时可区分的断言|默认画布线|',
                      '|---|---:|---:|---:|---:|',
                      f"|提交图|{base['nodes']}|{base['edges']}|"
                      f"{committed['relation_facts']['hypothetical_distinct_assertions']}|"
                      f"{committed['edges']['default_canvas_lines']}|"])
        for name, scenario in report['scenarios'].items():
            numbers = scenario.get('output')
            if numbers:
                lines.append(f"|{name}|{numbers['nodes']}|{numbers['raw_edges']}|"
                             f"{numbers['distinct_assertions']}|{numbers['default_canvas_lines']}|")
    differences = report.get('extra_vs_reference', {})
    ranked = sorted(report['scenarios'].get('exploratory', {}).get('rules', []),
                    key=lambda r: (-r['explained_net_nodes'], r['rule_id']))
    if ranked:
        lines.extend(['', '|规则|实体类型|指定键数|输出组数|按基线成员对回的净减少|', '|---|---|---:|---:|---:|'])
        for row in ranked:
            lines.append(f"|{row['rule_id'][-12:]}|{row['entity_type']}|{row['requested_observations']}|"
                         f"{row['output_groups']}|{row['explained_net_nodes']}|")
    if 'multiway' in report:
        m = report['multiway']
        lines.extend(['', f"跨表三方及以上同时匹配的假设，在当前提交图上有 {m['group_count']} 个不争用组，"
                      f"其中 {m['already_one_output']} 组已经同一输出。若目录同一性得到确认，按组计候选差额为 "
                      f"{m['hypothetical_node_reduction']} 个节点，详见 multiway.json。"])
    if differences:
        lines.extend(['', f"联合重放比指定清单额外减少 {differences['nodes_removed']} 个节点、"
                      f"{differences['raw_edges_removed']} 条原始边和 {differences['canvas_lines_removed']} 条画布线。"])
    if report.get('committed_only'):
        lines += ['', '本次按归档策略重放，节点与边的行摘要核对结果和原节点归属保存在 baseline.json。']
    else:
        lines += ['', '节点候选允许重叠；已排除、无法验证和有冲突的模式请查各检查点的 nodes.json、edges.json 与 hypotheses.json。',
                  'reference 使用指定的逐键清单。exploratory 另加入同族、同键列且两侧有相同类型值和来源名称的有限假设。两者均只模拟。',
                  '不同表是否共用业务目录仍需独立依据。关系组按粒度保留原边。详情中行号缺失的边另计。',
                  '独立裁决的精确率、召回率只适用于已标注的对象对及其抽取范围。']
    if report.get('errors'):
        lines.extend(['', '有阶段未完成，详见 summary.json 的 errors。'])
    (out/'report.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')


def _finish_campaign(output, report):
    report['status'] = 'incomplete' if report['errors'] else 'completed'
    write(output, 'summary.json', report)
    _report(output, report)
    return report


def campaign(run_file, node_id, output, workers, specs, excluded, families=(), census_only=False, committed_only=False):
    if committed_only and (census_only or specs):
        raise ValueError('--committed-only 单独核对已提交结果和原策略基线，不接受模拟或仅普查选项。')
    run, project, state = archived_state(run_file)
    record = state['artifacts'][node_id]
    if (record.get('run_id') != run['run_id'] or record.get('artifact_status') != 'committed'
            or state['merge_nodes'][node_id]['merge_mode'] not in {'deterministic', 'overlap_review'}):
        raise ValueError('请选择实际提交规则合并或重叠审查产物的归档运行。')
    parents = direct_merge_parents(state, record)
    families = list(families)
    if any(p.get('artifact_status') != 'committed' for p in parents):
        raise ValueError('上游产物未全部提交。')
    def location(r):
        return run_output_path(project, state.get('artifact_outputs', {}).get(r['node_id'], run['output_path']))
    output.mkdir(parents=True, exist_ok=False)
    report = {'run_id': run['run_id'], 'artifact_id': record['artifact_id'], 'node_id': node_id,
              'committed_merge_mode': state['merge_nodes'][node_id]['merge_mode'],
              'upstream_artifacts': record['upstream_artifacts'], 'workers': workers, 'model_calls': 0,
              'census_only': census_only, 'stages': {}, 'errors': {}, 'scenarios': {}}
    if committed_only:
        report['committed_only'] = True
    files = []
    if specs and not census_only:
        try:
            for path in specs:
                env = json.loads(read(path))
                if env.get('mode') != 'simulation_only':
                    raise ValueError('--simulation 需要 simulation_only 清单。')
                _simulated_policy(env, run, node_id, record, parents, {})
                files.append(env)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            report['errors']['reference_input'] = str(exc)
            files.clear()  # Never present part of a reference list as the full control.
    tasks = [(location(item), item, output/f'unit-{i+1}', True, excluded, families) for i, item in enumerate(parents)]
    tasks.append((location(record), record, output/'committed', False, excluded, families))
    try:
        stages, errors = run_stages(tasks, workers)
        report['stages'] = {item['node_id']: stages[item['node_id']] for item in [*parents, record] if item['node_id'] in stages}
        report['errors'].update(errors)
        if node_id in stages:
            checkpoint = json.loads(read(output/'committed'/'replay-input.json'))
            review = checkpoint.get('overlap_review')
            if review:
                report['historical_pattern_review'] = {k: review[k] for k in ('summary', 'model_review') if k in review}
        if not errors and not census_only:
            _, policy = committed_merge_policy(state, node_id, checkpoint)
            report['merge_policy_sha256'] = hashlib.sha256(canonical(policy).encode('utf-8')).hexdigest()
            inputs = [load_checkpoint(location(parent), parent) for parent in parents]
            if policy.get('verified_projection_rules'):
                from lightgraph.construction.projection_proposals import attach_checkpoint_grant
                from lightgraph.construction.source_namespaces import project_checkpoint_inputs
                inputs = project_checkpoint_inputs(inputs, run['project_id'])
                inputs = [attach_checkpoint_grant(c, location(p), p) for p, c in zip(parents, inputs, strict=True)]
            totals = {k: sum(len(i['graph'][k]) for i in inputs) for k in ('nodes', 'edges')}
            saved = state['merge_nodes'][node_id].get('merge_counts') or {}
            if any(saved.get('input_'+k, v) != v for k, v in totals.items()):
                raise ValueError('归档输入数量与已加载的上游图不一致。')
            report['input_rows'] = totals
            common = dict(run=run, node_id=node_id, target=record, parents=parents, policy=policy, inputs=inputs, committed=None, relation_families=families)
            if not committed_only:
                reference_rules = [*policy.get('reviewed_key_pairs', [])]
                for env in files:
                    reference_rules.extend(env['simulated_key_pairs'])
                evidence, hypotheses = discover(inputs, parents, run, record, reference_rules, excluded)
                evidence['excluded_values'] = excluded
                write(output, 'hypotheses.json', evidence)
                write(output, 'hypothesis-rules.json', hypotheses)
            print('核对原策略与提交图', flush=True)
            baseline = prepare_replay_baseline(inputs=inputs, policy=policy, target=record, committed_row_digests=checkpoint['row_digests'],
                                              relation_families=families)
            write(output, 'baseline.json', {'rows_match_committed': baseline.rows_match_committed, **baseline.snapshot.report})
            report['rows_match_committed'] = baseline.rows_match_committed
            base_report = baseline.snapshot.report
            report['baseline_checks'] = {k:base_report[k] for k in ('input_node_mapping_counts',
                'unverified_input_edge_occurrences', 'unverified_origin_bindings', 'integrity')}
            if (base_report['input_node_mapping_counts']['unverified'] or base_report['unverified_input_edge_occurrences']
                    or base_report['unverified_origin_bindings']
                    or any(count for key, count in base_report['integrity'].items() if key != 'self_links')):
                report['errors']['baseline_integrity_or_lineage'] = '核对 baseline_checks 的未核实项。'
            if not all(baseline.rows_match_committed.values()):
                raise ValueError('当前代码重放与提交图的节点或边逐行摘要不同，暂停净变化比较。')
            if committed_only:
                return _finish_campaign(output, report)
            multiway = summarize_multiway_outputs(evidence['multiway'], base_report['input_node_mapping'])
            write(output, 'multiway.json', multiway)
            report['multiway'] = {k:multiway[k] for k in ('group_count', 'hypothetical_node_reduction',
                                                           'already_one_output', 'exclusion_counts')}
            scenarios = [('reference', files)] if files else []
            if hypotheses or not files:
                scenarios.append(('exploratory', [*files, *hypotheses]))
            for name, bundles in scenarios:
                print(f'联合重放 {name}，规则清单 {len(bundles)} 份', flush=True)
                result = run_joint_replay(**common, baseline=baseline, simulation_paths=bundles)
                write(output, name+'.json', result)
                report['scenarios'][name] = {k: result[k] for k in ('status', 'delta', 'reason') if k in result}
                if 'simulated' in result:
                    after = result['simulated']
                    report['scenarios'][name]['rules'] = [
                        {k: row[k] for k in ('rule_id', 'entity_type', 'requested_observations', 'output_groups', 'explained_net_nodes')}
                        for row in result['rule_attribution']]
                    report['scenarios'][name]['output'] = {k: after[k] for k in ('nodes', 'raw_edges', 'default_canvas_lines')}
                    report['scenarios'][name]['output']['distinct_assertions'] = after['relation_facts']['hypothetical_distinct_assertions']
                if result['status'] not in {'completed', 'not_run_no_simulations'}:
                    report['errors'][name] = result['status']
            if files and not hypotheses and report['scenarios']['reference']['status'] == 'completed':
                report['scenarios']['exploratory'] = {'status': 'same_rules_as_reference',
                                                     **{k:v for k,v in report['scenarios']['reference'].items() if k in {'delta', 'output', 'rules'}}}
            ref = report['scenarios'].get('reference', {})
            union = report['scenarios'].get('exploratory', {})
            if ref.get('status') == 'completed' and union.get('status') in {'completed', 'same_rules_as_reference'}:
                report['extra_vs_reference'] = {k: union['delta'][k] - ref['delta'][k]
                                                for k in ('nodes_removed', 'raw_edges_removed', 'canvas_lines_removed')}
    except Exception as exc:
        report['errors']['campaign'] = f'{type(exc).__name__}: {exc}'
    return _finish_campaign(output, report)


def _input_mapping(section):
    return {(unit, nid): winner for unit, members in section['input_node_mapping'].items()
            for nid, winner in members.items()}


def score_saved(output, truth_path, result_file):
    summary = json.loads(read(output/'summary.json'))
    baseline_ids = json.loads(read(output/'committed'/'node-ids.json'))
    node_maps = {'committed': {n:n for n in baseline_ids}}
    input_maps = {}
    if (output/'baseline.json').exists():
        base = json.loads(read(output/'baseline.json'))
        if all(base['rows_match_committed'].values()):
            input_maps['committed'] = _input_mapping(base)
    relation_maps = {}
    for name, meta in summary['scenarios'].items():
        if meta['status'] in {'completed', 'completed_with_unverified'}:
            result = json.loads(read(output/(name+'.json')))
            node_maps[name] = result['node_mapping']
            input_maps[name] = _input_mapping(result['simulated'])
            relation_maps[name] = result['relation_scoring']
    if (summary['scenarios'].get('exploratory', {}).get('status') == 'same_rules_as_reference'
            and 'reference' in node_maps):
        node_maps['exploratory'] = node_maps['reference']
        input_maps['exploratory'] = input_maps['reference']
        relation_maps['exploratory'] = relation_maps['reference']
    edges = json.loads(read(output/'committed'/'edges.json'))
    facts = json.loads(read(output/'committed'/'relation-facts.json'))
    result = score(json.loads(read(truth_path)), summary['run_id'], summary['artifact_id'],
                   set(baseline_ids), node_maps, edges['edge_to_canvas_line'], input_maps, facts['edge_to_fact'],
                   relation_maps, json.loads(read(output/'committed'/'nodes.json'))['groups'])
    write(result_file.parent, result_file.name, result)


def _verified_commit(directory):
    summary = json.loads(read(directory/'summary.json'))
    baseline = json.loads(read(directory/'baseline.json'))
    stages = summary['stages']
    merged = stages[summary['node_id']]
    if (summary.get('census_only') or summary['committed_merge_mode'] not in {'deterministic', 'overlap_review'}
            or merged['kind'] != 'committed' or merged['node_id'] != summary['node_id']
            or merged['artifact_id'] != summary['artifact_id'] or not summary['run_id']
            or merged.get('checkpoint_run_id', summary['run_id']) != summary['run_id']
            or merged.get('artifact_status', 'committed') != 'committed'):
        raise ValueError('需要具有已提交合并产物和完整基线重放的普查目录。')
    row_digests = json.loads(read(directory/'committed'/'replay-input.json'))['row_digests']
    matches = {'nodes': True, 'edges': True}
    if (summary.get('rows_match_committed') != matches or baseline.get('rows_match_committed') != matches
            or set(row_digests) != set(matches) or baseline['row_digests'] != row_digests):
        raise ValueError('原策略重放的节点或边行摘要与本档提交图不符。')
    output_ids = set(json.loads(read(directory/'committed'/'node-ids.json')))
    if len(output_ids) != merged['rows']['nodes'] or baseline['nodes'] != len(output_ids):
        raise ValueError('提交图节点 ID 清单与节点数不符。')
    mapping = _input_mapping(baseline)
    upstream = summary['upstream_artifacts']
    units = {stage['artifact_id']: stage for stage in stages.values() if stage['kind'] == 'unit'}
    if not upstream or len(units) != len(upstream) or set(units) != set(upstream):
        raise ValueError('上游抽取产物清单与普查阶段不符。')
    expected = set()
    parents = []
    for number, artifact_id in enumerate(upstream, 1):
        stage = units[artifact_id]
        unit = stage['node_id']
        folder = f'unit-{number}'
        if (stage['directory'] != folder or stages[unit] != stage or not stage['graph_digest']
                or stage.get('artifact_status', 'committed') != 'committed'):
            raise ValueError('上游抽取单元或图摘要缺失。')
        ids = json.loads(read(directory/folder/'node-ids.json'))
        if len(ids) != stage['rows']['nodes']:
            raise ValueError('上游单元的节点数与其原节点 ID 清单不符。')
        if len(set(ids)) != len(ids):
            raise ValueError('同一上游单元的原节点 ID 重复，无法逐对确认成员。')
        expected.update((unit, node_id) for node_id in ids)
        parents.append({'node_id': unit, 'artifact_id': artifact_id, 'graph_digest': stage['graph_digest']})
    if mapping.keys() != expected or any(winner is not None and winner not in output_ids for winner in mapping.values()):
        raise ValueError('原节点映射未覆盖同一批输入，或映射目标不属于提交图。')
    summary_fields = ('run_id', 'node_id', 'artifact_id', 'committed_merge_mode')
    identity = {key: summary[key] for key in summary_fields}
    identity.update(graph_digest=merged['graph_digest'], row_digests=row_digests, rows=merged['rows'])
    return identity, parents, mapping


def compare_committed(first_dir, second_dir, truth_path, result_file):
    first, parents, first_map = _verified_commit(first_dir)
    second, other_parents, second_map = _verified_commit(second_dir)
    if first['run_id'] == second['run_id'] or first['artifact_id'] == second['artifact_id']:
        raise ValueError('请选择两次独立提交的运行与合并产物。')
    if first['node_id'] != second['node_id'] or parents != other_parents or first_map.keys() != second_map.keys():
        raise ValueError('两次提交必须使用同一合并节点、同一批有序上游检查点、图摘要和全部原节点 ID。')
    pairs = compare_input_pairs(json.loads(read(truth_path)), first, first_map, second_map)
    result = {'basis': 'two_committed_merges_same_inputs', 'model_calls': 0, 'first': first, 'second': second,
              'upstream_artifacts': parents, 'qualified_input_ids': len(first_map), **pairs}
    write(result_file.parent, result_file.name, result)


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    sub = cli.add_subparsers(dest='action', required=True)
    start = sub.add_parser('run', help='并行全量盘点，随后核算联合模拟结果')
    start.add_argument('run_file', type=Path)
    start.add_argument('--node', default='merge_1')
    start.add_argument('--output', type=Path, required=True, help='新的实验目录')
    start.add_argument('--workers', type=int, choices=range(1, 9), default=2)
    start.add_argument('--simulation', action='append', type=Path, default=[], help='已有逐键模拟清单，可多次传入')
    start.add_argument('--exclude-text', action='append', default=[], help='探索实验明确排除的文本哨兵，可多次传入')
    start.add_argument('--relation-families', type=Path, help='本次要检验的同义关系类型清单 JSON')
    mode = start.add_mutually_exclusive_group()
    mode.add_argument('--census-only', action='store_true')
    mode.add_argument('--committed-only', action='store_true', help='只普查并重放已提交策略取得成员映射，跳过探索和模拟')
    labels = sub.add_parser('score', help='在已保存的结果上核算独立标注，无需重新加载图')
    labels.add_argument('output_dir', type=Path)
    labels.add_argument('--truth', type=Path, required=True)
    labels.add_argument('--result', type=Path, required=True)
    comparison = sub.add_parser('compare-committed', help='按同一批抽取原节点的裁决，对照两个实际提交的合并结果')
    comparison.add_argument('first_dir', type=Path, help='第一份 run 普查结果目录，裁决清单应绑定此运行和产物')
    comparison.add_argument('second_dir', type=Path, help='第二份 run 普查结果目录，复用相同上游检查点')
    comparison.add_argument('--truth', type=Path, required=True)
    comparison.add_argument('--result', type=Path, required=True)
    edges = sub.add_parser('compare-source-relations', help='按冻结来源行、端点及适用条件逐边对照两批检查点')
    edges.add_argument('old_run', type=Path)
    edges.add_argument('new_run', type=Path)
    edges.add_argument('--node', default='merge_1')
    edges.add_argument('--result', type=Path, required=True, help='保存本机明细的新文件')
    args = cli.parse_args()
    if args.action == 'score':
        score_saved(args.output_dir, args.truth, args.result)
        return 0
    if args.action == 'compare-committed':
        try:
            compare_committed(args.first_dir, args.second_dir, args.truth, args.result)
        except (KeyError, OSError, TypeError, ValueError) as exc:
            cli.error(str(exc))
        return 0
    if args.action == 'compare-source-relations':
        from fusion_edge_comparison import compare_source_relations
        try:
            if args.result.exists():
                raise FileExistsError('请指定新的核对结果文件。')
            result = compare_source_relations(args.old_run, args.new_run, args.node)
            write(args.result.parent, args.result.name, result)
        except (KeyError, OSError, TypeError, ValueError) as exc:
            cli.error(str(exc))
        return 0
    report = campaign(args.run_file, args.node, args.output, args.workers, args.simulation, args.exclude_text,
                      json.loads(read(args.relation_families)) if args.relation_families else [],
                      args.census_only, args.committed_only)
    print(json.dumps({'status': report['status'], 'output': str(args.output),
                      'errors': report['errors']}, ensure_ascii=False))
    return int(bool(report['errors']))


if __name__ == '__main__':
    sys.exit(main())
