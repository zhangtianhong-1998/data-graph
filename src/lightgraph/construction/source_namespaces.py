"""Physical source addresses are comparable only within their declared project namespace."""
from collections import defaultdict

from lightgraph.contracts import digest


BINDING = 'physical_source_namespace_binding'


def source_namespace(inputs):
    """A caller binds a local project. A prior publication can rule that binding out."""
    if not inputs or not all(isinstance(c, dict) and isinstance(c.get('graph'), dict)
                             and isinstance(c.get('contributor_id'), str) for c in inputs):
        return None
    names, groups = namespace_groups(inputs)
    return names[0] if names and len(groups) == 1 else None


def namespace_groups(contributors):
    if not any('source_namespace' in c or BINDING in (c.get('graph') or {}) for c in contributors):
        return None, [list(range(len(contributors)))]
    names, groups = [], defaultdict(list)
    for i, contributor in enumerate(contributors):
        value = contributor.get('source_namespace')
        name = value if isinstance(value, str) and value.strip() else None
        binding = (contributor.get('graph') or {}).get(BINDING)
        if binding and (not name or binding != digest('project_source_namespace_v1', name)):
            name = None
        names.append(name)
        key = ('declared', name) if name else ('unknown', contributor.get('kind', 'unit'), contributor['contributor_id'])
        groups[key].append(i)
    return names, list(groups.values())


def record_namespace(graph, names, groups):
    if names is not None:
        graph[BINDING] = (digest('project_source_namespace_v1', names[0])
                          if len(groups) == 1 and names[0] else 'unverified')


def isolate_physical_ids(prepared, targets, observed, dictionary, groups, names, excluded_types):
    """Two separate projects can mint the same physical ID, including in an earlier publication."""
    uses = defaultdict(lambda: defaultdict(list))
    for group_number, indices in enumerate(groups):
        for i in indices:
            _, kind, graph = prepared[i]
            definitions = {d['type']: d for d in graph.get('definitions', {}).get('entities', [])}
            for node in graph.get('nodes', []):
                raw = node['id']
                frozen_local = (kind in {'merged', 'base'} and node['type'] not in excluded_types
                                and node.get('original_type') not in excluded_types
                                and definitions.get(node['type'], {}).get('identity_scope') != 'global')
                if raw in targets[i] or node.get('identity_origin') == 'physical_column' or frozen_local:
                    uses[targets[i].get(raw, raw)][group_number].append((i, raw))
    for shared, by_group in uses.items():
        if len(by_group) < 2:
            continue
        ordered = sorted(by_group.values(), key=lambda entries: (
            all(prepared[i][1] not in {'merged', 'base'} for i, _ in entries),
            min(i for i, _ in entries)))
        for entries in ordered[1:]:
            first = entries[0][0]
            namespace = ['declared', names[first]] if names[first] else ['unknown', *prepared[first][:2]]
            fresh = 'n_' + digest('project_source_identity_v1', namespace, shared)
            for i, raw in entries:
                targets[i][raw] = fresh
                for group_targets in (observed, dictionary):
                    if raw in group_targets[i]:
                        group_targets[i][raw] = fresh
