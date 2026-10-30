"""Read-only graph scoring, extracted from the independent fruit evaluation.

All code values are explicit Gold data. No business prefixes, fixed paths,
model calls, fuzzy label identity, or experiment-specific row counts are used.
"""
from __future__ import annotations
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from .models import Costs, EvaluationReport, GoldGraph, ModeReport, Score


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def fingerprint(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def ratio(n, d):
    return round(n / d, 8) if d else None


def score(tp, predicted, expected):
    return Score(true_positive=tp, false_positive=predicted-tp, false_negative=expected-tp,
                 predicted=predicted, expected=expected, precision=ratio(tp, predicted),
                 recall=ratio(tp, expected), f1=ratio(2*tp, predicted+expected),
                 exact=tp == predicted == expected)


def scalars(value):
    if isinstance(value, dict):
        for item in value.values(): yield from scalars(item)
    elif isinstance(value, list):
        for item in value: yield from scalars(item)
    else: yield value


def scoped_nodes(graph, statuses, kinds):
    nodes = {n['id']: dict(n) for n in graph.get('nodes', []) if n.get('status') in statuses
             and n.get('layer') == 'business' and (not kinds or n.get('kind') in kinds)}
    if 'facts' not in graph:
        return nodes
    values = defaultdict(list)
    for fact in graph['facts']:
        if fact.get('status') in statuses and fact.get('fact_kind') == 'PropertyValue':
            p = fact['payload']
            if p['entity_id'] in nodes and p['value'] not in values[p['entity_id'], p['name']]:
                values[p['entity_id'], p['name']].append(p['value'])
    for node in nodes.values():
        node.update(properties={}, aliases=[], label=node['id'])
    for (node_id, name), items in values.items():
        value = items[0] if len(items) == 1 else items
        if name == 'alias': nodes[node_id]['aliases'] = items
        elif name == 'label': nodes[node_id]['label'] = value
        else: nodes[node_id]['properties'][name] = value
    return nodes


def binding_namespaces(store, graph):
    if store is None or not Path(store).exists(): return {}, 'not_supplied'
    needed = sorted({s['source_binding'] for s in graph.get('supports', []) if s.get('source_binding')})
    result = {}
    try:
        with sqlite3.connect(Path(store).resolve().as_uri() + '?mode=ro', uri=True) as connection:
            for offset in range(0, len(needed), 400):
                batch = needed[offset:offset+400]
                query = 'SELECT binding_id,namespace_ref FROM source_binding WHERE binding_id IN (' + ','.join('?' * len(batch)) + ')'
                for binding, namespace in connection.execute(query, batch):
                    for _ in range(3):
                        if not isinstance(namespace, str): break
                        try: namespace = json.loads(namespace)
                        except ValueError: break
                    if isinstance(namespace, dict): result[binding] = namespace
    except sqlite3.Error: return {}, 'store_namespace_unavailable'
    return result, 'read_only_result_store'


def evaluate_mode(graph, gold, statuses, namespaces):
    nodes = scoped_nodes(graph, statuses, gold.entity_kinds)
    id_counts=Counter(n['id'] for n in graph.get('nodes',[]) if n.get('status') in statuses
        and n.get('layer')=='business' and (not gold.entity_kinds or n.get('kind') in gold.entity_kinds))
    expected = {n.code: n for n in gold.nodes}
    per_code, codes_by_node = defaultdict(set), {}
    invalid = set()
    for nid, node in nodes.items():
        values = list(scalars(node.get('properties', {}).get(gold.code_property, [])))
        codes = {v for v in values if isinstance(v, str) and v}
        codes_by_node[nid] = codes
        for code in codes: per_code[code].add(nid)
        if len(codes) != 1 or id_counts[nid]!=1 or any(not isinstance(v, str) or not v for v in values): invalid.add(nid)
    unique = {nid: next(iter(codes)) for nid, codes in codes_by_node.items() if nid not in invalid
              and next(iter(codes)) in expected and len(per_code[next(iter(codes))]) == 1
              and (expected[next(iter(codes))].kind is None or nodes[nid].get('kind') == expected[next(iter(codes))].kind)}
    supports = defaultdict(list)
    for item in graph.get('supports', []):
        if item.get('status') in statuses: supports[item['id']].append(item)
    identity_supports = defaultdict(set)
    for fact in graph.get('facts', []):
        if fact.get('status') in statuses and fact.get('fact_kind') == 'NodeExistence':
            identity_supports[fact['payload']['entity_id']].update(fact.get('support_ids', []))
    scoped, wrong, unverified = set(), [], []
    for nid, code in unique.items():
        wanted = set(expected[code].subjects)
        if not wanted: continue  # Scope is not scored when Gold declares no scope.
        observed = []
        for sid in identity_supports[nid]:
            for support in supports[sid]:
                namespace = namespaces.get(support.get('source_binding'), {})
                seen = {v for col, v in zip(namespace.get('namespace_columns', []), namespace.get('namespace_values', []))
                        if isinstance(v, str) and (col == gold.namespace_column or isinstance(col, str) and col.endswith('.'+gold.namespace_column))}
                if support.get('scope_ref') in wanted: seen.add(support['scope_ref'])
                observed.append(seen)
        combined=set().union(*observed) if observed else set()
        if any(v - wanted for v in observed): wrong.append(nid)
        elif observed and all(observed) and combined==wanted: scoped.add(nid)
        else: unverified.append(nid)
    expected_edges = {(e.source, e.target, e.relation) for e in gold.edges}
    projected, edge_count, mapped_count, invalid_edges = set(), 0, 0, 0
    scored_relations = {e.relation for e in gold.edges} | set(gold.parent_relations) | set(gold.child_relations)
    for edge in graph.get('edges', []):
        if edge.get('status') not in statuses: continue
        relation = edge.get('relation', edge.get('relation_kind'))
        if relation not in scored_relations: continue
        if gold.entity_kinds and edge.get('source') not in nodes and edge.get('target') not in nodes: continue
        edge_count += 1
        a, b = edge.get('source'), edge.get('target')
        if a not in unique or b not in unique:
            invalid_edges += 1
            continue
        if relation in gold.child_relations: a, b, relation = b, a, 'parent_of'
        elif relation in gold.parent_relations: relation = 'parent_of'
        projected.add((unique[a], unique[b], relation)); mapped_count += 1
    expected_names = {(n.code, value) for n in gold.nodes for value in n.english_names}
    expected_labels = {(n.code, value) for n in gold.nodes for value in n.labels}
    expected_positions = {(n.code, value) for n in gold.nodes for value in n.positions}
    names, positions = set(), set()
    labels = set()
    name_count = position_count = 0
    for nid, code in unique.items():
        node = nodes[nid]
        if expected[code].labels:
            labels.update((code,v) for v in scalars(node.get('label',[])) if isinstance(v,str))
        # Labels/aliases can recover expected English text but are not themselves
        # asserted English attributes. Incorrect explicit english_name values are FP.
        explicit = {v for v in scalars(node.get('properties', {}).get('english_name', [])) if isinstance(v, str)}
        other = {v for v in scalars([node.get('aliases', []), node.get('label')]) if isinstance(v, str)}
        observed_names = explicit | (other & set(expected[code].english_names))
        names.update((code, v) for v in observed_names); name_count += len(observed_names)
        observed_positions = {v for v in scalars(node.get('properties', {}).get('source_position', [])) if type(v) is int}
        positions.update((code, v) for v in observed_positions); position_count += len(observed_positions)
    return ModeReport(statuses=sorted(statuses), identities=score(len(unique), sum(id_counts.values()), len(expected)),
        edges=score(len(projected & expected_edges), edge_count, len(expected_edges)),
        english_names=score(len(names & expected_names), name_count, len(expected_names)),
        labels=score(len(labels & expected_labels), len(labels), len(expected_labels)),
        positions=score(len(positions & expected_positions), position_count, len(expected_positions)),
        subject_verified_identities=score(len(scoped), sum(bool(expected[c].subjects) for c in unique.values()), sum(bool(n.subjects) for n in gold.nodes)),
        duplicates={c: sorted(ids) for c, ids in per_code.items() if len(ids)>1},
        duplicate_node_ids=sorted(nid for nid,count in id_counts.items() if count>1),
        merged_node_ids=sorted(n for n, c in codes_by_node.items() if len(c)>1),
        unmapped_node_ids=sorted(set(nodes)-set(unique)), unknown_codes=sorted(set(per_code)-set(expected)),
        invalid_endpoint_edges=invalid_edges, duplicate_edge_instances=mapped_count-len(projected),
        unverified_scope_node_ids=sorted(unverified), wrong_scope_node_ids=sorted(wrong))


def evaluate(graph_path, gold_path, *, store=None, run_report=None, usage=None, manifest=None):
    path = Path(graph_path)
    if path.is_dir(): path /= 'graph.json'
    graph = load(path) if path.exists() else {'nodes': [], 'edges': []}
    gold = GoldGraph.model_validate(load(gold_path))
    namespace, namespace_evidence = binding_namespaces(store, graph)
    report = load(run_report) if run_report else {}
    usage_data = load(usage) if usage else {}
    usage_data = usage_data.get('usage', usage_data)
    metadata = load(manifest) if manifest else {}
    def number(value, integer=False):
        if type(value) not in (int, float) or value < 0: return None
        return int(value) if integer else value
    calls, inputs, outputs = [number(usage_data.get(k), True) for k in ('calls', 'input_tokens', 'output_tokens')]
    issues = Counter(i['code'] for i in report.get('issues', []) if isinstance(i, dict)
                     and isinstance(i.get('code'), str) and re.fullmatch(r'[a-z][a-z0-9_]{0,127}', i['code']))
    unresolved = {k: v for k, v in issues.items() if any(part in k for part in ('unresolved', 'incomplete', 'pending', 'no_confirmable', 'invalid_drafts', 'deferred', 'exhausted'))}
    return EvaluationReport(status='SCORED' if path.exists() else 'GRAPH_FILE_MISSING',
        graph_sha256=fingerprint(path) if path.exists() else None, gold_sha256=fingerprint(gold_path),
        accepted=evaluate_mode(graph, gold, {'accepted'}, namespace),
        accepted_plus_candidates=evaluate_mode(graph, gold, {'accepted', 'candidate', 'unresolved'}, namespace),
        costs=Costs(calls=calls, input_tokens=inputs, output_tokens=outputs,
                    total_tokens=inputs+outputs if inputs is not None and outputs is not None else None,
                    elapsed_seconds=number(metadata.get('elapsed_seconds'))),
        run_status=report.get('status') if report.get('status') in ('COMPLETED','COMPLETED_WITH_GAPS','PARTIAL','FAILED') else None,
        issue_counts=dict(issues), unresolved_counts=unresolved, namespace_evidence=namespace_evidence)
