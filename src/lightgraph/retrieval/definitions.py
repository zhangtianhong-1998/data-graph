"""Read published type explanations separately from facts asserted by individual rows."""
from __future__ import annotations

import json
from collections import defaultdict
from contextlib import closing

from .store_sqlite import normalize_term


def indexed_definitions(graph, views, nodes, edges):
    """Unqualified historical description text can only speak for the whole visible type."""
    cohorts = defaultdict(set)
    for row in views:
        cohorts['entities', nodes[row['node_id']]['kind']].add(row['scope_ref'])
    for row in edges:
        cohorts['relations', row['rel_type']].add(row['scope_ref'])
    for category in ('entities', 'relations'):
        for item in graph.get('definitions', {}).get(category, []):
            kind = item['type']
            text = item.get('description')
            required = cohorts.get((category, kind))
            if not required or not isinstance(text, str) or not text.strip():
                continue
            contract = {k: item[k] for k in ('key_names', 'identity_scope', 'identity_context_keys',
                                              'source_type', 'target_type', 'hierarchy_orientation') if item.get(k)}
            yield category, kind, text, normalize_term(text), json.dumps(contract, ensure_ascii=False), json.dumps(sorted(required))


def _example(index, conn, category, kind, cohorts, ids):
    # A visible relation requires both endpoint grants, as in traversal itself.
    allowed = 'SELECT value FROM json_each(?)'
    id_filter = ''
    if category == 'entities':
        sql = f"""SELECT v.node_id, json_extract(v.observations_json, '$[0]') AS observation
                  FROM kg_nodes n JOIN kg_node_scope_views v ON v.node_id=n.node_id
                  WHERE n.kind=? AND v.scope_ref IN ({allowed})"""
        id_column = 'v.node_id'
        params = [kind, cohorts]
    else:
        sql = f"""SELECT e.edge_id, json_extract(e.observations_json, '$[0]') AS observation FROM ({index._authorized_edges_sql(allowed)}) e
                  WHERE e.rel_type=?"""
        id_column = 'e.edge_id'
        params = [cohorts, cohorts, cohorts, kind]
    if ids:
        id_filter = f' AND {id_column} IN ({allowed})'
        params.append(json.dumps(ids))
    return conn.execute(sql + id_filter + ' ORDER BY ' + id_column + ', scope_ref LIMIT 1', params).fetchone()


def definitions_for_answer(retriever, answer, scopes, request, limit=24):
    """Return cited schema text for returned types or a literal definition lookup."""
    index = retriever.index
    if not index.has_type_definitions:
        return []
    plan = answer.get('plan') or {}
    if plan.get('scope_unresolved'):
        return []
    selection = plan.get('business_context', request.business_context)
    allowed = json.dumps(sorted(retriever.authorized_cohorts(scopes, selection)))
    anchors = defaultdict(list)
    partial = answer.get('available_context') or {}
    for section in (answer, partial):
        for node in section.get('nodes', ()):
            anchors['entities', node['kind']].append(node['id'])
        for edge in section.get('edges', ()):
            anchors['relations', edge['relation']].append(edge['id'])
    # A definition hit is a schema reference. It never binds a query slot or
    # establishes an observation about an instance.
    terms = [gap['text'] for gap in plan.get('unbound_mentions', ()) if gap.get('text')]
    lookup_text = request.mode == 'auto' or (request.mode == 'search' and request.field != 'name' and request.method != 'vector')
    if not anchors and not terms and lookup_text and request.query.strip():
        terms = [request.query]
    terms = list(dict.fromkeys(normalize_term(term) for term in terms if normalize_term(term)))
    if not terms and not anchors:
        return []
    results = []
    with closing(index._connect()) as conn:
        rows = conn.execute("""SELECT * FROM kg_type_definitions d
            WHERE NOT EXISTS (SELECT 1 FROM json_each(d.required_cohorts) g
                              WHERE g.value NOT IN (SELECT value FROM json_each(?)))
            ORDER BY category, kind""", (allowed,))
        for row in rows:
            key = (row['category'], row['kind'])
            by_text = any(term in row['normalized'] for term in terms)
            if key not in anchors and not by_text:
                continue
            example = _example(index, conn, *key, allowed, anchors[key])
            if example is None or not example['observation']:
                continue
            if len(results) == limit:
                answer['type_definitions_truncated'] = True
                break
            observation = json.loads(example['observation'])
            provenance = observation.get('provenance') or {}
            binding = {k: observation[k] for k in ('business_context', 'source_tables', 'evidence_refs', 'observation_id')
                       if observation.get(k)}
            binding.update({k: provenance[k] for k in ('definition', 'mapping', 'source', 'key_columns',
                                                        'source_key_columns', 'target_key_columns', 'predicate')
                            if k in provenance})
            binding['node_id' if key[0] == 'entities' else 'edge_id'] = example[0]
            results.append({'source': 'published_type_definition', 'category': key[0], 'type': key[1],
                            'description': row['description'], 'contract': json.loads(row['contract']),
                            'text_match': by_text, 'example_binding': binding})
    return results
