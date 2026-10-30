"""SQL paging over current authorized supports, before Python aggregation."""
import json
from lightgraph.contracts import digest
from lightgraph.errors import LightGraphError


def graph_page(store,scopes,domain,*,layer='business',include_candidates=False,offset=0,limit=240,q='',kind=''):
    if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=1000 or len(q)>256:
        raise LightGraphError('graph_page_invalid')
    if layer not in ('business','metadata'):raise LightGraphError('graph_layer_invalid')
    # Preserve all existing cluster/edge validity checks when identity decisions
    # exist. The common undecided graph uses SQL-only selection and aggregation.
    exists=store.connection.execute("SELECT 1 FROM sqlite_master WHERE name='identity_decision'").fetchone()
    active=exists and store.connection.execute('SELECT 1 FROM identity_decision WHERE active=1 AND domain=? LIMIT 1',(domain,)).fetchone()
    if active:
        view=store.view(scopes,domain,include_candidates=include_candidates)
        allnodes=[n for n in view['nodes'] if n['layer']==layer]
        matching=[n for n in allnodes if (not kind or n['kind']==kind) and
                  (not q or q.casefold() in json.dumps(n,ensure_ascii=False).casefold())]
        nodes=matching[offset:offset+limit];ids={n['id'] for n in nodes};allids={n['id'] for n in allnodes}
        edges=[e for e in view['edges'] if e['source'] in allids and e['target'] in allids]
        selected=[e for e in edges if e['source'] in ids and e['target'] in ids]
        return {**view,'nodes':nodes,'edges':selected,'total_nodes':len(allnodes),'total_edges':len(edges),
            'matched_nodes':len(matching),'next_offset':offset+limit if offset+limit<len(matching) else None,
            'kinds':sorted({n['kind'] for n in allnodes}),'boundary_edges':sum((e['source'] in ids) != (e['target'] in ids) for e in edges),
            'offset':offset,'page_size':limit,'truncated':False,'query_strategy':'validated_identity_projection',
            'generation':digest(view['publications'])}
    scopes=sorted(scopes);states=['accepted','candidate','unresolved'] if include_candidates else ['accepted']
    scope_sql=','.join('?' for _ in scopes) or 'NULL';state_sql=','.join('?' for _ in states)
    base=f'''WITH visible AS MATERIALIZED (
      SELECT gf.*,fs.support_id,fs.origin,fs.binding_id,fs.scope_ref,fs.partition_id,fs.rule_id,fs.revision,
        ps.publication_id,ps.validation_ref,ps.document_ref,ps.binding_revision,ps.status
      FROM partition_head h JOIN publication p USING(publication_id)
      JOIN publication_support ps USING(publication_id) JOIN fact_support fs USING(support_id)
      JOIN graph_fact gf USING(fact_id)
      WHERE h.valid=1 AND p.state='published' AND gf.isolation_domain=?
        AND fs.scope_ref IN ({scope_sql}) AND ps.status IN ({state_sql})),
      nodes AS (SELECT DISTINCT json_extract(payload,'$.entity_id') id,json_extract(payload,'$.kind') kind
        FROM visible WHERE fact_kind='NodeExistence' AND json_extract(payload,'$.layer')=?),
      edges AS (SELECT DISTINCT fact_id,json_extract(payload,'$.source') src,json_extract(payload,'$.target') dst
        FROM visible WHERE fact_kind='Edge' AND json_extract(payload,'$.source') IN (SELECT id FROM nodes)
        AND json_extract(payload,'$.target') IN (SELECT id FROM nodes)),
      matched AS (SELECT * FROM nodes n WHERE (?='' OR n.kind=?) AND (?='' OR instr(lower(n.id),lower(?))>0
        OR EXISTS (SELECT 1 FROM visible v WHERE v.fact_kind='PropertyValue' AND json_extract(v.payload,'$.entity_id')=n.id
           AND instr(lower(CAST(json_extract(v.payload,'$.value') AS TEXT)),lower(?))>0))),
      page AS (SELECT * FROM matched ORDER BY id LIMIT ? OFFSET ?) '''
    args=[domain,*scopes,*states,layer,kind,kind,q,q,q,limit,offset]
    with store._lock:
        store.connection.execute('BEGIN')
        try:
            counts=store.connection.execute(base+'''SELECT (SELECT count(*) FROM nodes),
              (SELECT count(*) FROM edges),(SELECT count(*) FROM matched),
              (SELECT count(*) FROM edges WHERE (src IN (SELECT id FROM page)) != (dst IN (SELECT id FROM page)))''',args).fetchone()
            rows=store.connection.execute(base+'''SELECT * FROM visible WHERE
              (fact_kind IN ('NodeExistence','PropertyValue') AND json_extract(payload,'$.entity_id') IN (SELECT id FROM page)) OR
              (fact_kind='Edge' AND json_extract(payload,'$.source') IN (SELECT id FROM page) AND json_extract(payload,'$.target') IN (SELECT id FROM page))
              ORDER BY fact_id,support_id,publication_id''',args).fetchall()
            kinds=[r[0] for r in store.connection.execute(base+'SELECT DISTINCT kind FROM nodes ORDER BY kind',args)]
            publications=[r[0] for r in store.connection.execute(base+'SELECT DISTINCT publication_id FROM visible ORDER BY publication_id',args)]
            result=store._aggregate(rows)
            return {**result,'total_nodes':counts[0],'total_edges':counts[1],'matched_nodes':counts[2],
                'boundary_edges':counts[3],'kinds':kinds,'offset':offset,'page_size':limit,
                'next_offset':offset+limit if offset+limit<counts[2] else None,'truncated':False,
                'generation':digest(publications),'query_strategy':'authorized_sql_page'}
        finally:store.connection.execute('ROLLBACK')
