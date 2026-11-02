"""Presentation vocabulary from the exact rules supporting an authorized page."""
import json


def display_schema(store, graph):
    """Avoid exposing unused definitions or loading another complete graph view."""
    usage = {
        'entity_types': {n.get('kind') for n in graph.get('nodes', [])},
        'relations': {e.get('relation') for e in graph.get('edges', [])},
        'properties': {f['payload'].get('name') for f in graph.get('facts', [])
                       if f.get('fact_kind') == 'PropertyValue'},
    }
    for fact in graph.get('facts', []):
        payload = fact['payload']
        if fact.get('fact_kind') == 'NodeExistence':
            usage['entity_types'].add(payload.get('kind'))
        elif fact.get('fact_kind') == 'Edge':
            usage['relations'].add(payload.get('relation'))
    versions = {(s['rule_id'], s['rule_revision']) for s in graph.get('supports', [])}
    ids = sorted({rid for rid, _ in versions})
    definitions = {category: {} for category in usage}
    for start in range(0, len(ids), 300):
        batch = ids[start:start + 300]
        rows = store.connection.execute(
            'SELECT rule_id,revision,payload FROM rule WHERE rule_id IN (' +
            ','.join('?' for _ in batch) + ') ORDER BY rule_id,revision', batch)
        for rid, revision, payload in rows:
            if (rid, revision) not in versions:
                continue
            schema = json.loads(payload).get('semantic_schema', {})
            for category, names in usage.items():
                for item in schema.get(category, []):
                    if item['name'] in names:
                        definitions[category].setdefault(item['name'], {
                            'name': item['name'], 'description': item['description']})
    return {category: list(entries.values()) for category, entries in definitions.items()}


def endpoint_captions(nodes, facts):
    """Only captions for endpoints referenced by this bounded, authorized fact page."""
    ids = {f['payload'].get(key) for f in facts for key in ('entity_id', 'source', 'target')}
    fields = ('id', 'label', 'display_label', 'name', 'kind', 'layer', 'status', 'scopes')
    return [{key: node[key] for key in fields if key in node}
            for node in nodes if node['id'] in ids]
