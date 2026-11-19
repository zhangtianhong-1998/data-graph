"""Use version-matched communities and centrality as a retrieval primitive."""
from collections import defaultdict

from .dimensional import empty_answer


def retrieve_overview(retriever, query, scopes, plan, max_nodes=80, max_edges=150):
    result = empty_answer(query, scopes, plan)
    selection = plan['business_context']
    analysis = retriever.analytics_loader(scopes, selection) if retriever.analytics_loader else None
    if analysis is None:
        result['overview'] = {'status': 'analysis_not_ready', 'communities': []}
        return result

    communities = analysis['communities']
    by_community = defaultdict(list)
    for node in analysis['nodes']:  # Sorted by PageRank when the analysis is persisted.
        by_community[node['community_id']].append(node)
    selected = communities[:min(8, max_nodes)]
    requested = plan['overview_metric']
    ranking_only = requested != 'community'
    metric = requested if ranking_only else 'pagerank'
    if ranking_only:
        # An explicit centrality question ranks the entire authorized projection,
        # including important nodes in smaller communities.
        ranked = sorted(analysis['nodes'], key=lambda n: (-n[metric], n['id']))
        representatives = ranked[:min(10, max_nodes)]
        group_ids = {n['community_id'] for n in representatives}
        selected = [c for c in communities if c['id'] in group_ids]
    else:
        representatives = []
        for position in range(3):
            for community in selected:
                members = by_community[community['id']]
                if position < len(members) and len(representatives) < max_nodes:
                    representatives.append(members[position])
    ids = [node['id'] for node in representatives]
    stats = {node['id']: node for node in representatives}
    if ids:
        cohorts = retriever.authorized_cohorts(scopes, selection)
        graph = retriever.index.expand_neighborhood(ids, scopes=cohorts, hops=1, max_nodes=len(ids),
            max_edges=max_edges, max_per_rel=max_edges, preferred_neighbor_ids=ids)
        result['nodes'] = [{**graph['nodes'][nid], **stats[nid],
                            'display_name': graph['nodes'][nid]['display_name'], 'is_seed': False,
                            'is_bridge': False, 'matched_slots': []} for nid in ids if nid in graph['nodes']]
        result['edges'] = graph['edges']
        # Communities are full-graph statistics; representatives in the
        # returned subgraph may have no direct edge between them.
        adjacent = {nid: set() for nid in graph['nodes']}
        for edge in graph['edges']:
            adjacent[edge['source']].add(edge['target'])
            adjacent[edge['target']].add(edge['source'])
        for nid in ids:
            if nid not in adjacent:
                continue
            stack, component = [nid], []
            while stack:
                current = stack.pop()
                if current in adjacent:
                    component.append(current)
                    stack.extend(adjacent.pop(current))
            result['connected_components'].append(sorted(component))
        result['truncated'] = 'max_edges_reached' in graph['truncation_reasons']
        if result['truncated']:
            result['truncation_reasons'] = ['max_edges_reached']
    result.update(version_id=analysis['version_id'], overview={
        'status': analysis['status'], 'counts': analysis['counts'], 'communities': selected,
        'ranking_metric': metric, 'selection': 'top_centrality' if ranking_only else 'community_representatives',
        'sampled': len(result['nodes']) < analysis['counts']['nodes'], 'representative_count': len(result['nodes']), 'algorithm_version': analysis['algorithm_version']})
    return result
