"""Deterministic statistics for a caller-authorized graph; no LLM estimates."""
from collections import Counter, defaultdict

from lightgraph.contracts import canonical
from lightgraph.source_values import persisted_identity_key


def directed_topology(node_ids, edges):
    """Count full directed strongly connected components without recursion.

    Kahn leftovers include descendants of a cycle. Iterative Kosaraju identifies
    only actual cycle members and handles long chains without recursion limits.
    """
    nodes = set(node_ids)
    outgoing, incoming = defaultdict(set), defaultdict(set)
    pairs = {(edge['source'], edge['target']) for edge in edges
             if edge['source'] in nodes and edge['target'] in nodes}
    for source, target in pairs:
        outgoing[source].add(target)
        incoming[target].add(source)
    seen, order = set(), []
    for start in sorted(nodes):
        if start in seen:
            continue
        seen.add(start)
        stack = [(start, iter(sorted(outgoing[start])))]
        while stack:
            current, targets = stack[-1]
            target = next(targets, None)
            if target is None:
                order.append(current)
                stack.pop()
            elif target not in seen:
                seen.add(target)
                stack.append((target, iter(sorted(outgoing[target]))))
    seen, cyclic, component_of, cyclic_components = set(), set(), {}, 0
    for start in reversed(order):
        if start in seen:
            continue
        seen.add(start)
        stack, component = [start], set()
        while stack:
            current = stack.pop()
            component.add(current)
            for previous in incoming[current]:
                if previous not in seen:
                    seen.add(previous)
                    stack.append(previous)
        if len(component) > 1 or (start, start) in pairs:
            cyclic_components += 1
            cyclic.update(component)
            for node in component:
                component_of[node] = cyclic_components
    return {
        'nodes': len(nodes), 'edges': len(edges), 'distinct_endpoint_pairs': len(pairs),
        'roots': sum(not incoming[node] for node in nodes),
        'nodes_without_outgoing_edges': sum(not outgoing[node] for node in nodes),
        'isolated_nodes': sum(not incoming[node] and not outgoing[node] for node in nodes),
        'nodes_with_incoming_and_outgoing': sum(bool(incoming[node]) and bool(outgoing[node]) for node in nodes),
        'multiple_parent_nodes': sum(len(incoming[node]) > 1 for node in nodes),
        'self_links': sum(edge['source'] == edge['target'] for edge in edges),
        'acyclic': not cyclic, 'cycle_components': cyclic_components, 'cycle_nodes': len(cyclic),
        'cycle_edges': sum(source in cyclic and target in cyclic and component_of[source] == component_of[target]
                           for source, target in pairs),
    }


def graph_statistics(graph):
    """Compute only from supplied nodes/edges; callers must scope-filter first."""
    nodes, edges = graph.get('nodes', []), graph.get('edges', [])
    ids = {node['id'] for node in nodes}
    kinds = sorted({node['type'] for node in nodes})
    relations = sorted({edge['type'] for edge in edges})
    entity_counts, relation_counts = {}, {}

    def mapping_keys(items):
        return {canonical({key: origin.get(key) for key in ('definition', 'mapping', 'source')})
                for item in items for origin in item.get('provenance', [])}

    for kind in kinds:
        subset = [node for node in nodes if node['type'] == kind]
        aliases = [str(alias) for node in subset for alias in set(node.get('aliases', []))]
        labels = Counter(node.get('display_label', node.get('label', '')) for node in subset)
        source_names = {s['label'] for node in subset for s in node.get('supports', []) if s.get('label') is not None}
        entity_counts[kind] = {
            'nodes': len(subset), 'unique_labels': len(labels), 'unique_source_labels': len(source_names),
            'duplicate_label_groups': sum(count > 1 for count in labels.values()),
            'nodes_with_duplicate_labels': sum(count for count in labels.values() if count > 1),
            'aliases': len(aliases), 'unique_aliases': len(set(aliases)),
            'nodes_with_multiple_aliases': sum(len(set(n.get('aliases', []))) > 1 for n in subset),
            'nodes_with_multiple_mappings': sum(len(mapping_keys([n])) > 1 for n in subset),
            'observed_mappings': len(mapping_keys(subset)),
            'label_origins': dict(sorted(Counter(n.get('label_origin', 'unknown') for n in subset).items())),
        }
    relation_defs = {d['type']: d for d in graph.get('definitions', {}).get('relations', [])}
    for kind in relations:
        subset = [edge for edge in edges if edge['type'] == kind]
        definition = relation_defs.get(kind, {})
        endpoint_types = {definition.get('source_type'), definition.get('target_type')} - {None}
        endpoints = ({n['id'] for n in nodes if n['type'] in endpoint_types} if endpoint_types else
                     {endpoint for e in subset for endpoint in (e['source'], e['target'])} & ids)
        relation_counts[kind] = {**directed_topology(endpoints, subset), 'observed_mappings': len(mapping_keys(subset)),
                                'acyclic_declared': bool(definition.get('acyclic', False))}
    return {
        'complete': True, 'basis': 'provided_graph_after_visibility_filter',
        'nodes': len(nodes), 'edges': len(edges), 'entity_type_count': len(kinds), 'relation_type_count': len(relations),
        'aliases': sum(item['aliases'] for item in entity_counts.values()),
        'source_row_contexts': len({support['row_context']['id'] for item in nodes + edges
                                   for support in item.get('supports', []) if support.get('row_context')}),
        'observed_mappings': {'entities': len(mapping_keys(nodes)), 'relations': len(mapping_keys(edges))},
        'entity_types': entity_counts, 'relation_types': relation_counts,
        'topology': directed_topology(ids, edges),
        'integrity': {
            'duplicate_node_ids': len(nodes) - len(ids),
            'duplicate_edge_ids': len(edges) - len({e['id'] for e in edges}),
            'duplicate_node_identities': len(nodes) - len({(n['type'], persisted_identity_key(n)) for n in nodes}),
            'duplicate_edge_endpoints': len(edges) - len({(e['type'], e['source'], e['target']) for e in edges}),
            'dangling_edges': sum(e['source'] not in ids or e['target'] not in ids for e in edges),
        },
    }
