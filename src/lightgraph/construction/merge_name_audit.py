"""Read-only accounting for one exact name at the input and output of a merge."""
from collections import Counter, defaultdict
from decimal import InvalidOperation

from lightgraph.contracts import canonical
from lightgraph.errors import LightGraphError
from lightgraph.source_values import identity_cell


def _typed_key(node, definition):
    # Across projections a single key may have different column names. Compare its
    # typed value for counting only. Preserve field names on composite keys.
    keys = definition.get('key_names') or []
    identity = node.get('identity') or {}
    types = node.get('identity_types') or {}
    if (node.get('identity_origin', definition.get('identity_origin', 'row')) != 'row'
            or not keys or set(keys) != set(identity) or set(keys) != set(types)
            or any(identity[k] in (None, '') for k in keys)):
        return None
    try:
        cells = {k: identity_cell(node, k) for k in keys}
    except (ValueError, TypeError, InvalidOperation, LightGraphError):
        return None
    if any(kind != types[k] for k, (kind, _) in cells.items()):
        return None
    if len(keys) == 1:
        kind, value = cells[keys[0]]
        return {'value_type': kind, 'value': value}
    return {'fields': {k: {'value_type': t, 'value': v} for k, (t, v) in sorted(cells.items())}}


def name_rows(graph, name, *, include_drafts=False):
    """Select exact stored label, display label or a name observed in a mapped column."""
    definitions = {d['type']: d for d in graph.get('definitions', {}).get('entities', [])}
    rows = []
    for node in graph['nodes']:
        display = node.get('display_label') or node.get('label')
        sources = node.get('supports') or []
        observed = set()
        incomplete = not sources
        for support in sources:
            label = support.get('observed_label') or support.get('label')
            if (support.get('provenance') or {}).get('label_column') and isinstance(label, str) and label.strip():
                observed.add(label)
            else:
                incomplete = True
        if display != name and node.get('label') != name and name not in observed:
            continue
        row = {'node_id': node['id'], 'type': node['type'], 'label': node.get('label'),
               'display_label': display, 'observed_names': sorted(observed), 'incomplete_name_evidence': incomplete,
               'typed_key': _typed_key(node, definitions.get(node['type'], {}))}
        if include_drafts:
            versions = set()
            unknown = not sources
            for support in sources:
                for origin in support.get('execution_origins') or [support.get('validation_origin') or {}]:
                    draft = origin.get('draft_id')
                    if draft:
                        versions.add((draft, origin.get('draft_revision', origin.get('revision'))))
                    else:
                        unknown = True
            row['draft_versions'] = [{'draft_id': draft, 'revision': version}
                                     for draft, version in sorted(versions, key=canonical)]
            row['missing_draft_attribution'] = unknown
        rows.append(row)
    return sorted(rows, key=lambda r: r['node_id'])


def _counts(rows, name):
    # Counters overlap intentionally: a node can match both its display and source name.
    return {'matched_nodes': len(rows),
            'exact_label_nodes': sum(r['label'] == name for r in rows),
            'exact_display_nodes': sum(r['display_label'] == name for r in rows),
            'exact_observed_nodes': sum(name in r['observed_names'] for r in rows),
            'multiple_observed_names_nodes': sum(len(r['observed_names']) > 1 for r in rows),
            'incomplete_name_evidence_nodes': sum(r['incomplete_name_evidence'] for r in rows),
            'missing_typed_key_nodes': sum(r['typed_key'] is None for r in rows)}


def name_population(rows, name):
    groups = defaultdict(list)
    for row in rows:
        if row['typed_key'] is not None:
            groups[canonical(row['typed_key'])].append(row)
    by_key = []
    for _, members in sorted(groups.items()):
        name_sets = Counter(tuple(r['observed_names']) for r in members)
        by_key.append({'typed_key': members[0]['typed_key'], **_counts(members, name),
                       'by_observed_names': [{'names': list(names), 'nodes': count}
                                             for names, count in sorted(name_sets.items())]})
    return {'name': name, **_counts(rows, name), 'distinct_typed_keys': len(groups),
            'distinct_typed_keys_with_exact_label': sum(k['exact_label_nodes'] > 0 for k in by_key),
            'by_typed_key': by_key}


class MergeNameAudit:
    """Use the merger's contributor-qualified lineage even when original IDs collide."""

    def __init__(self, inputs, name):
        self.name = name
        self.by_owner = {c['contributor_id']: name_rows(c['graph'], name, include_drafts=True) for c in inputs}
        self.input_rows = {(owner, row['node_id']): row for owner, rows in self.by_owner.items() for row in rows}

    def inputs(self):
        contributors = {}
        for owner, rows in sorted(self.by_owner.items()):
            drafts = Counter(canonical(version) for row in rows for version in row['draft_versions'])
            versions = {canonical(v): v for row in rows for v in row['draft_versions']}
            contributors[owner] = {**name_population(rows, self.name),
                                   'nodes_missing_draft_attribution': sum(r['missing_draft_attribution'] for r in rows),
                                   'by_draft_version': [{**versions[key], 'matching_input_nodes': count}
                                                        for key, count in sorted(drafts.items())],
                                   'nodes': rows}
        return {**name_population(list(self.input_rows.values()), self.name), 'by_contributor': contributors}

    def replay(self, graph, lineage):
        rows = name_rows(graph, self.name)
        grouped = defaultdict(list)
        cohorts = defaultdict(list)
        mapping = []
        for key, row in sorted(self.input_rows.items()):
            owner, original_id = key
            target = lineage.get(key)
            status = 'mapped' if target is not None else 'no_output' if key in lineage else 'unverified'
            mapping.append({'contributor_id': owner, 'input_node_id': original_id, 'output_node_id': target,
                            'status': status})
            if row['typed_key'] is not None:
                bucket = (canonical(row['typed_key']), tuple(row['observed_names']), row['incomplete_name_evidence'])
                cohorts[bucket].append((owner, target, status, row['typed_key']))
            if target is not None:
                grouped[target].append(owner)
        by_key_names = []
        for (_, names, incomplete), members in sorted(cohorts.items()):
            owners = Counter(m[0] for m in members)
            by_key_names.append({'typed_key': members[0][3], 'observed_names': list(names),
                                 'incomplete_name_evidence': incomplete, 'input_nodes': len(members),
                                 'input_nodes_by_contributor': dict(sorted(owners.items())),
                                 'distinct_output_nodes': len({m[1] for m in members if m[1] is not None}),
                                 'unverified_inputs': sum(m[2] == 'unverified' for m in members),
                                 'inputs_without_output': sum(m[2] == 'no_output' for m in members)})
        scope = Counter()
        for owners in grouped.values():
            distinct = len(set(owners))
            if len(owners) == 1:
                kind = 'single_input'
            elif distinct == 1:
                kind = 'within_one_contributor'
            elif len(owners) == distinct:
                kind = 'across_contributors'
            else:
                kind = 'within_and_across_contributors'
            scope[kind] += 1
        output_ids = {row['node_id'] for row in rows}
        matched = set(grouped)
        return {**name_population(rows, self.name), 'mapped_input_nodes': sum(map(len, grouped.values())),
                'distinct_outputs_of_matching_inputs': len(matched),
                'matching_outputs_without_matching_input': len(output_ids - matched),
                'mapped_outputs_outside_name_selection': len(matched - output_ids),
                'outputs_by_matching_input_contributors': dict(sorted(scope.items())),
                'input_to_output_by_key_and_names': by_key_names, 'input_mapping': mapping}
