"""Whole-graph relationship census. All IDs refer to the supplied (local) graph."""
from collections import Counter, defaultdict

from lightgraph.construction.relation_assertions import (
    assertion_signature, interval_union, support_address, support_assertion,
)
from lightgraph.contracts import digest
from lightgraph.hierarchy_bindings import observed_level_pairs
from lightgraph.web.graph_payload import hierarchy_display, relation_definitions
from lightgraph.worksheet_witness import worksheet_row_witness


def _worksheet_rows(edges):
    """Compare complete observed row sets, keeping snapshots and source addresses separate."""
    signatures = []
    located = incomplete = invalid = 0
    for edge in edges:
        grouped_spans = defaultdict(list)
        supports = edge.get('supports') or []
        incomplete += not supports
        for support in supports:
            witness = worksheet_row_witness(support.get('source_row_witness'))
            address = support_address(support)
            if witness and address:
                grouped_spans[(*address, witness['snapshot'])].extend(witness['rows'])
                located += 1
            elif support.get('source_row_witness') is not None:
                invalid += 1
            else:
                incomplete += 1
        signature = []
        for location, spans in sorted(grouped_spans.items()):
            signature.append((location, interval_union(spans)))
        signatures.append(tuple(signature))
    if invalid:
        return 'invalid_locator_or_source_address'
    if not located:
        return 'no_recorded_physical_rows'
    if incomplete:
        return 'partial_physical_rows'
    return 'same_recorded_physical_rows' if len(set(signatures)) == 1 else 'different_recorded_row_sets'


def _canvas_key(edge, display):
    """Mirror workbench graphRelationLines.ts: hidden loop, separate edge, or grouped line."""
    flow, key = display.get('hierarchy_direction'), display.get('hierarchy_line_key')
    if edge['source'] == edge['target'] and flow in ('incoming', 'outgoing'):
        return None
    return (edge['source'], edge['target'], flow, key) if edge['id'] and key else ()


def _canvas_line_id(key, edge_id):
    if key is None:
        return None
    return 'hierarchy_' + digest(key) if key else 'edge_' + edge_id


def canvas_counts(graph, *, observe_line=None):
    """Lightweight whole-graph default canvas counters for comparing simulated outputs."""
    definitions = relation_definitions(graph)
    lines = set()
    loops = separate = 0
    for edge in graph['edges']:
        key = _canvas_key(edge, hierarchy_display(edge, definitions))
        if observe_line is not None:
            observe_line(edge['id'], _canvas_line_id(key, edge['id']))
        if key is None:
            loops += 1
        elif key:
            lines.add(key)
        else:
            separate += 1
    drawable = separate + len(lines)
    count = len(graph['edges'])
    return {'raw_edges': count, 'default_canvas_lines': drawable, 'hierarchy_self_loops': loops,
            'hierarchy_line_edge_savings': count - loops - drawable}


def _endpoint_lines(edges, displays):
    groups = defaultdict(list)
    for i, (edge, display) in enumerate(zip(edges, displays, strict=True)):
        key = _canvas_key(edge, display)
        if key is not None:
            groups[key or (i,)].append(edge)
    return list(groups.values())


def _physical_slots(edge):
    pairs = observed_level_pairs(edge['type'], edge.get('supports') or [])
    return tuple(sorted({
        (address['source_id'], address['schema'], address['table'], src, dst)
        for address, src, dst in pairs
    }))


def _base_type(edge, definitions):
    return edge.get('original_type') or definitions.get(edge['type'], {}).get('original_type') or edge['type']


def analyze_edges(graph):
    """Partition raw edge occurrences by directed endpoints; retain overlapping secondary views separately."""
    definitions = relation_definitions(graph)
    nodes = {n['id']: n.get('label') for n in graph['nodes']}
    endpoints, names, answers = defaultdict(list), defaultdict(list), defaultdict(list)
    unsupported_names = 0
    for edge in graph['edges']:
        source, target = edge['source'], edge['target']
        endpoints[source, target].append(edge)
        labels = (nodes.get(source), nodes.get(target))
        if all(isinstance(label, str) and label.strip() for label in labels):
            names[_base_type(edge, definitions), *labels].append(edge)
        else:
            unsupported_names += 1

    multi, loops, edge_to_line = [], [], {}
    lines_count = hierarchy_loops = exact_extra = exact_groups = exact_participants = 0
    physical_slots_differ = same_base_multi = different_type_multi = bidirectional = 0
    grouped_lines, grouped_edges = Counter(), Counter()
    for (source, target), edges in sorted(endpoints.items()):
        edges.sort(key=lambda e: (e['type'], e['id']))
        displays = [hierarchy_display(e, definitions) for e in edges]
        for edge, display in zip(edges, displays, strict=True):
            if edge['id'] in edge_to_line:
                raise ValueError('Cannot trace canvas lines: duplicate relation ID in input graph')
            flow = display.get('hierarchy_direction')
            key = _canvas_key(edge, display)
            edge_to_line[edge['id']] = _canvas_line_id(key, edge['id'])
            if source == target:
                hierarchy = flow in ('incoming', 'outgoing')
                hierarchy_loops += hierarchy
                loops.append({'source': source, 'edge_id': edge['id'], 'type': edge['type'], 'hierarchy': hierarchy})
            elif flow in ('incoming', 'outgoing'):
                pair = (source, target) if flow == 'outgoing' else (target, source)
                answers[pair].append(edge['id'])
        lines = _endpoint_lines(edges, displays)
        lines_count += len(lines)
        bidirectional += source < target and (target, source) in endpoints
        if len(edges) < 2:
            continue

        types = Counter(e['type'] for e in edges)
        bases = Counter(_base_type(e, definitions) for e in edges)
        exact_extra += sum(n - 1 for n in types.values())
        exact_groups += sum(n > 1 for n in types.values())
        exact_participants += sum(n for n in types.values() if n > 1)
        same_base_multi += any(n > 1 for n in bases.values())
        different_type_multi += len(types) > 1
        bindings = [_physical_slots(e) for e in edges]
        distinct_slots = {slot for slot in bindings if slot}
        physical_slots_differ += len(distinct_slots) > 1
        line_details = []
        for line in lines:
            detail = {'edge_ids': [e['id'] for e in line]}
            if len(line) > 1:
                basis = _worksheet_rows(line)
                grouped_lines[basis] += 1
                grouped_edges[basis] += len(line)
                detail['row_evidence'] = basis
            line_details.append(detail)
        multi.append({
            'source': source, 'target': target, 'edges': [
                {'id': e['id'], 'type': e['type'], 'physical_level_bindings': slot,
                 'contributors': sorted({s['contributor_id'] for s in e.get('supports') or [] if s.get('contributor_id')})}
                for e, slot in zip(edges, bindings, strict=True)],
            'row_evidence': _worksheet_rows(edges), 'drawable_lines': line_details,
        })

    same_label_groups = [
        {'base_type': base, 'source_label': src, 'target_label': dst, 'edge_ids': sorted(e['id'] for e in group),
         'distinct_directed_endpoint_pairs': len({(e['source'], e['target']) for e in group})}
        for (base, src, dst), group in sorted(names.items()) if len(group) > 1]
    raw = len(graph['edges'])
    summary = {
        'raw_edges': raw, 'directed_endpoint_pairs': len(endpoints), 'multi_endpoint_pairs': len(multi),
        'multi_endpoint_edges': sum(len(g['edges']) for g in multi), 'extra_edges_at_same_endpoints': raw - len(endpoints),
        'exact_typed_endpoint_excess': exact_extra, 'exact_typed_endpoint_groups': exact_groups,
        'exact_typed_endpoint_edges': exact_participants, 'distinct_type_multi_endpoint_pairs': different_type_multi,
        'repeated_base_type_endpoint_pairs': same_base_multi,
        'different_observed_level_binding_sets_at_same_endpoints': physical_slots_differ,
        'bidirectional_endpoint_pairs_excluding_loops': bidirectional,
        'self_loops': len(loops), 'hierarchy_self_loops': hierarchy_loops, 'default_canvas_lines': lines_count,
        'hierarchy_line_edge_savings': raw - hierarchy_loops - lines_count,
        'grouped_lines_by_row_evidence': dict(grouped_lines), 'grouped_raw_edges_by_row_evidence': dict(grouped_edges),
        'multi_endpoint_pairs_by_row_evidence': dict(Counter(g['row_evidence'] for g in multi)),
        'hierarchy_answer_pairs': len(answers), 'hierarchy_answer_evidence_edges': sum(map(len, answers.values())),
        'same_base_type_and_endpoint_label_groups': len(same_label_groups),
        'same_base_type_and_endpoint_label_excess': sum(len(g['edge_ids']) - 1 for g in same_label_groups),
        'edges_without_both_endpoint_labels': unsupported_names,
    }
    return {'summary': summary, 'multi_endpoint_groups': multi, 'loop_edges': loops, 'same_label_groups': same_label_groups,
            'edge_to_canvas_line': edge_to_line,
            # Structural candidate answers; an actual query also checks access and business-context compatibility.
            'multi_edge_hierarchy_answer_candidates': [
                {'parent': parent, 'child': child, 'edge_ids': sorted(ids)}
                for (parent, child), ids in sorted(answers.items()) if len(ids) > 1]}


def _declared_families(families):
    if not isinstance(families, list):
        raise ValueError('Relation equivalence families must be a list')
    names, mapping = set(), {}
    for family in families:
        if not isinstance(family, dict) or set(family) != {'family', 'types'}:
            raise ValueError('Each relation family must contain family and types')
        name, types = family['family'], family['types']
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError('Invalid or repeated relation family name')
        if (not isinstance(types, list) or len(types) < 2 or not all(isinstance(t, str) and t.strip() for t in types)
                or len(set(types)) != len(types) or any(t in mapping for t in types)):
            raise ValueError('Use two or more distinct base types per family, with no overlapping families')
        names.add(name)
        mapping.update(dict.fromkeys(types, name))
    return mapping


def _physical_row_proofs(edge):
    """Compact locators for one representative; all confirmed members have identical qualified row slices."""
    spans, proofs = defaultdict(list), {}
    for support in edge['supports']:
        _, qualifications, witness = support_assertion(support)
        qualifier = digest(qualifications)
        key = qualifier, witness['snapshot']
        spans[key].extend(witness['rows'])
        address, src, dst = qualifications['endpoint_bindings']
        proofs[key] = {'source': dict(zip(('source_id', 'schema', 'table'), address, strict=True)),
                       'source_columns_with_ordinals': src, 'target_columns_with_ordinals': dst,
                       'snapshot': witness['snapshot'], 'qualifications_sha256': qualifier}
    return [{**proofs[key], 'rows': interval_union(spans[key])} for key in sorted(proofs)]


def analyze_relation_facts(graph, families):
    """Simulate a distinct-assertion view for declared relation synonyms. Raw edges are read only."""
    declared = _declared_families(families)
    definitions = relation_definitions(graph)
    endpoints, fact_for = defaultdict(list), {}
    present_types = set()
    for edge in graph['edges']:
        if edge['id'] in fact_for:
            raise ValueError('Cannot trace relationship assertions: duplicate relation ID in input graph')
        fact_for[edge['id']] = 'edge_' + edge['id']
        kind = _base_type(edge, definitions)
        present_types.add(kind)
        if kind in declared:
            endpoints[declared[kind], edge['source'], edge['target']].append(edge)

    confirmed, pending = [], []
    evaluated_edges = evaluated_groups = 0
    for (family, source, target), members in sorted(endpoints.items()):
        if len({_base_type(e, definitions) for e in members}) < 2:
            continue
        evaluated_groups += 1
        evaluated_edges += len(members)
        profiles, missing, facets = defaultdict(list), defaultdict(list), defaultdict(set)
        for edge in members:
            reason, key, evidence = assertion_signature(edge, definitions.get(edge['type']))
            if reason:
                missing[reason].append(edge['id'])
            else:
                profiles[key].append(edge)
                for name, value in evidence.items():
                    facets[name].add(value)
        block_facts = []
        for key, group in sorted(profiles.items()):
            if len({_base_type(e, definitions) for e in group}) < 2:
                continue
            ids = sorted(e['id'] for e in group)
            fact = 'fact_' + digest(family, source, target, key)
            fact_for.update(dict.fromkeys(ids, fact))
            block_facts.append(fact)
            confirmed.append({'fact_id': fact, 'family': family, 'source': source, 'target': target,
                              'original_edge_ids': ids, 'evidence_digest': key,
                              'physical_row_proofs': _physical_row_proofs(group[0]),
                              'supports': sum(len(e['supports']) for e in group)})
        if len({fact_for[e['id']] for e in members}) > 1:
            disagreements = sorted(name for name, values in facets.items() if len(values) > 1)
            if len(profiles) > 1 and not disagreements:
                disagreements.append('row_to_qualifier_binding')
            pending.append({'family': family, 'source': source, 'target': target,
                            'original_edge_ids': sorted(e['id'] for e in members), 'confirmed_subgroups': block_facts,
                            'incomplete_edges_by_reason': {r: sorted(ids) for r, ids in sorted(missing.items())},
                            'differing_evidence': disagreements})
    participants = sum(len(g['original_edge_ids']) for g in confirmed)
    reduction = participants - len(confirmed)
    summary = {'raw_edges': len(fact_for), 'declared_families': len(families),
               'declared_types_missing_from_graph': sorted(declared.keys() - present_types),
               'evaluated_multi_type_endpoint_groups': evaluated_groups, 'evaluated_raw_edges': evaluated_edges,
               'confirmed_fact_groups': len(confirmed), 'confirmed_fact_edges': participants, 'confirmed_reduction': reduction,
               'hypothetical_distinct_assertions': len(fact_for) - reduction, 'candidate_groups_with_unresolved_pairs': len(pending)}
    return {'summary': summary, 'confirmed_groups': confirmed, 'candidate_groups': pending, 'edge_to_fact': fact_for}
