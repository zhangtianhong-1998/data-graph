"""SQL paging over current authorized supports, before Python aggregation."""
import json
from lightgraph.contracts import digest
from lightgraph.errors import LightGraphError


def graph_page(store,scopes,domain,*,layer='business',include_candidates=False,offset=0,limit=240,q='',kind='',focus='',hops=1):
    if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=1000 or len(q)>256 or not isinstance(focus,str) or len(focus)>256 or type(hops) is not int or hops not in (1,2):
        raise LightGraphError('graph_page_invalid')
    if focus:limit=min(limit,999)  # The anchor counts toward the 1000-node response cap.
    # Two-hop pages retain the earlier prefix so second-hop nodes never lose
    # their connecting first-hop edges when the browser extends the view.
    two_hops=bool(focus) and hops==2
    end=min(offset+limit,999) if two_hops else offset+limit
    page_offset=0 if two_hops else offset
    page_limit=end if two_hops else limit
    if layer not in ('business','metadata'):raise LightGraphError('graph_layer_invalid')
    # Preserve all existing cluster/edge validity checks when identity decisions
    # exist. The common undecided graph uses SQL-only selection and aggregation.
    exists=store.connection.execute("SELECT 1 FROM sqlite_master WHERE name='identity_decision'").fetchone()
    active=exists and store.connection.execute('SELECT 1 FROM identity_decision WHERE active=1 AND domain=? LIMIT 1',(domain,)).fetchone()
    if active:
        view=store.view(scopes,domain,include_candidates=include_candidates)
        allnodes=[n for n in view['nodes'] if n['layer']==layer]
        allids={n['id'] for n in allnodes}
        edges=[e for e in view['edges'] if e['source'] in allids and e['target'] in allids]
        adjacency={nid:set() for nid in allids}
        for edge in edges:
            adjacency[edge['source']].add(edge['target'])
            adjacency[edge['target']].add(edge['source'])
        first=adjacency.get(focus,set())
        neighbors=set(first)
        if two_hops:
            for nid in first:neighbors.update(adjacency[nid])
        matching=[n for n in allnodes if (not focus or n['id'] in neighbors-{focus}) and (not kind or n['kind']==kind) and
                  (not q or q.casefold() in json.dumps(n,ensure_ascii=False).casefold())]
        matching.sort(key=lambda n:(n['id'] not in first if two_hops else False,n['id']))
        nodes=matching[page_offset:page_offset+page_limit]
        if focus in allids:nodes=[n for n in allnodes if n['id']==focus]+[n for n in nodes if n['id']!=focus]
        ids={n['id'] for n in nodes}
        selected=[e for e in edges if e['source'] in ids and e['target'] in ids]
        return {**view,'nodes':nodes,'edges':selected,'total_nodes':len(allnodes),'total_edges':len(edges),
            'matched_nodes':len(matching),'next_offset':end if end<len(matching) and (not two_hops or end<999) else None,
            'kinds':sorted({n['kind'] for n in allnodes}),
            'kind_counts':{k:sum(n['kind']==k for n in allnodes) for k in {n['kind'] for n in allnodes}},
            'focus_node_id':focus if focus in allids else None,'focus_neighbor_count':len(neighbors-{focus}),'focus_hops':hops,
            'boundary_edges':sum((e['source'] in ids) != (e['target'] in ids) for e in edges),
            'neighborhood_truncated':two_hops and len(matching)>999,
            'offset':offset,'page_size':limit,'truncated':two_hops and len(matching)>999,'query_strategy':'validated_identity_projection',
            'generation':digest({'publications':view['publications'],'nodes':sorted(allids)})}
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
      anchor AS (SELECT id FROM nodes WHERE id=?),
      first_hop AS (SELECT dst id FROM edges WHERE src IN (SELECT id FROM anchor)
        UNION SELECT src id FROM edges WHERE dst IN (SELECT id FROM anchor)),
      neighbors AS (SELECT id FROM first_hop
        UNION SELECT dst id FROM edges WHERE ?=2 AND src IN (SELECT id FROM first_hop)
        UNION SELECT src id FROM edges WHERE ?=2 AND dst IN (SELECT id FROM first_hop)),
      matched AS (SELECT * FROM nodes n WHERE (?='' OR (n.id!=? AND n.id IN (SELECT id FROM neighbors))) AND (?='' OR n.kind=?) AND (?='' OR instr(lower(n.id),lower(?))>0
        OR EXISTS (SELECT 1 FROM visible v WHERE v.fact_kind='PropertyValue' AND json_extract(v.payload,'$.entity_id')=n.id
           AND instr(lower(CAST(json_extract(v.payload,'$.value') AS TEXT)),lower(?))>0))),
      chunk AS (SELECT * FROM matched ORDER BY CASE WHEN id IN (SELECT id FROM first_hop) THEN 0 ELSE 1 END,id LIMIT ? OFFSET ?),
      page AS (SELECT * FROM chunk UNION SELECT * FROM nodes WHERE id=?) '''
    args=[domain,*scopes,*states,layer,focus,hops,hops,focus,focus,kind,kind,q,q,q,page_limit,page_offset,focus]
    with store._lock:
        store.connection.execute('BEGIN')
        try:
            counts=store.connection.execute(base+'''SELECT (SELECT count(*) FROM nodes),
              (SELECT count(*) FROM edges),(SELECT count(*) FROM matched),
              (SELECT count(*) FROM edges WHERE (src IN (SELECT id FROM page)) != (dst IN (SELECT id FROM page))),
              (SELECT count(*) FROM neighbors WHERE id NOT IN (SELECT id FROM anchor))''',args).fetchone()
            rows=store.connection.execute(base+'''SELECT * FROM visible WHERE
              (fact_kind IN ('NodeExistence','PropertyValue') AND json_extract(payload,'$.entity_id') IN (SELECT id FROM page)) OR
              (fact_kind='Edge' AND json_extract(payload,'$.source') IN (SELECT id FROM page) AND json_extract(payload,'$.target') IN (SELECT id FROM page))
              ORDER BY fact_id,support_id,publication_id''',args).fetchall()
            kind_counts={r[0]:r[1] for r in store.connection.execute(base+'SELECT kind,count(*) FROM nodes GROUP BY kind ORDER BY kind',args)}
            kinds=list(kind_counts)
            publications=[r[0] for r in store.connection.execute(base+'SELECT DISTINCT publication_id FROM visible ORDER BY publication_id',args)]
            result=store._aggregate(rows)
            from .display import composite_columns, display_rules, project_display_labels
            contracts=display_rules(store,result)
            concept_ids={r['rule_id'] for r in contracts.values() if r.get('kind')=='concept_mapping'}
            context_ids={r['rule_id'] for r in contracts.values() if composite_columns(r)}|concept_ids
            if context_ids:
                # Fetch only accepted role endpoints of this page. Captions can
                # use off-page names without returning off-page nodes/facts.
                refs=list(concept_ids)
                for r in store.connection.execute('SELECT rule_id,payload FROM rule'):
                    rule=json.loads(r[1])
                    if rule.get('kind')=='reference' and (rule.get('relation') or {}).get('source_rule_id') in context_ids:
                        refs.append(r[0])
                if refs:
                    marks=','.join('?' for _ in set(refs))
                    extra=store.connection.execute(base+f''', display_edges AS (
                      SELECT * FROM visible WHERE status='accepted' AND fact_kind='Edge'
                      AND json_extract(payload,'$.source') IN (SELECT id FROM page) AND rule_id IN ({marks}))
                      SELECT * FROM visible WHERE status='accepted' AND (
                        fact_id IN (SELECT fact_id FROM display_edges) OR
                        (fact_kind IN ('NodeExistence','PropertyValue') AND json_extract(payload,'$.entity_id') IN
                          (SELECT json_extract(payload,'$.target') FROM display_edges))) LIMIT 20001''',
                      [*args,*sorted(set(refs))]).fetchall()
                    if len(extra)<=20000:
                        basis=store._aggregate([*rows,*extra])
                        project_display_labels(basis,display_rules(store,basis))
                        captions={n['id']:n for n in basis['nodes'] if 'display_label' in n}
                        for node in result['nodes']:
                            if node['id'] in captions:
                                for key in ('display_label','display_label_basis'):
                                    node[key]=captions[node['id']][key]
            return {**result,'total_nodes':counts[0],'total_edges':counts[1],'matched_nodes':counts[2],
                'boundary_edges':counts[3],'kinds':kinds,'kind_counts':kind_counts,
                'focus_node_id':focus if any(n['id']==focus for n in result['nodes']) else None,
                'focus_neighbor_count':counts[4] if focus else 0,'focus_hops':hops,'offset':offset,'page_size':limit,
                'next_offset':end if end<counts[2] and (not two_hops or end<999) else None,
                'neighborhood_truncated':two_hops and counts[2]>999,'truncated':two_hops and counts[2]>999,
                'generation':digest(publications),'query_strategy':'authorized_sql_page'}
        finally:store.connection.execute('ROLLBACK')
