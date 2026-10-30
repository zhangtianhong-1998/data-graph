"""Bounded graph review and reversible, scope-bound identity decisions.

Original facts and bindings remain immutable. Current views apply only decisions
whose evidence is still visible; history never inherits a later decision.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import json
import unicodedata
import uuid

from lightgraph.contracts import GraphFact, canonical, digest
from lightgraph.errors import LightGraphError


@dataclass
class GenerationView:
    heads: list[dict]
    graph: dict
    publications: list[str]

    @classmethod
    def capture(cls, store, replacements, scopes, domain='default'):
        """Read a complete prospective graph without exposing staging as current."""
        with store._lock:
            store.connection.execute('BEGIN')
            try:
                pubs=[store._publication(p) for p in replacements]
                if any(p['state'] not in ('staging','ready') for p in pubs):
                    raise LightGraphError('generation_requires_staging')
                ids=store._prospective_publications(pubs)
                store._validate_endpoints(ids)
                rows=[r for r in store._fact_rows(ids) if r['isolation_domain']==domain and r['scope_ref'] in scopes
                      and r['status'] in ('accepted','candidate','unresolved')]
                return cls(store.heads(),store._aggregate(rows),ids)
            finally:
                store.connection.execute('ROLLBACK')


def _initialize(store):
    store.connection.executescript('''
    CREATE TABLE IF NOT EXISTS resolution_version (singleton INTEGER PRIMARY KEY, revision INTEGER NOT NULL);
    INSERT OR IGNORE INTO resolution_version VALUES(1,0);
    CREATE TABLE IF NOT EXISTS identity_candidate (
      candidate_id TEXT PRIMARY KEY, domain TEXT NOT NULL, scopes TEXT NOT NULL,
      members TEXT NOT NULL, evidence TEXT NOT NULL, channels TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS identity_decision (
      decision_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, domain TEXT NOT NULL,
      scopes TEXT NOT NULL, members TEXT NOT NULL, evidence TEXT NOT NULL,
      verdict TEXT NOT NULL, origin TEXT NOT NULL, revision INTEGER NOT NULL, active INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS identity_review_attempt (
      candidate_id TEXT PRIMARY KEY, dependencies TEXT NOT NULL, outcome TEXT NOT NULL);
    ''')
    columns={r[1] for r in store.connection.execute('PRAGMA table_info(identity_decision)')}
    if 'goal_revision' not in columns:
        store.connection.execute('ALTER TABLE identity_decision ADD COLUMN goal_revision INTEGER NOT NULL DEFAULT 0')
    if 'rule_dependencies' not in columns:
        store.connection.execute("ALTER TABLE identity_decision ADD COLUMN rule_dependencies TEXT NOT NULL DEFAULT '[]'")


def _exists(store):
    return store.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='identity_decision'").fetchone() is not None


def _normalized(value):
    return ' '.join(unicodedata.normalize('NFKC',str(value)).casefold().split())


def find_candidates(graph, *, per_entity=8, bucket_limit=32, max_candidates=256, max_entities=5000):
    """Bound index buckets before pair generation, including common-name cases."""
    eligible=sorted((n for n in graph['nodes'] if n['layer']=='business'),key=lambda n:n['id'])
    nodes={n['id']:n for n in eligible[:max_entities]}
    tokens=defaultdict(set);evidence=defaultdict(set);index=defaultdict(list)
    omitted=set();channels=defaultdict(set)
    for fact in graph['facts']:
        payload=fact['payload'];entity=payload.get('entity_id')
        if entity not in nodes:continue
        if fact['fact_kind']=='NodeExistence':evidence[entity].add(fact['id'])
        if fact['fact_kind']!='PropertyValue' or payload['name'] not in ('label','alias','english_name','business_code','definition'):
            continue
        value=_normalized(payload['value'])
        if not value or len(value)>1024:continue
        channel='code' if payload['name']=='business_code' else 'definition' if payload['name']=='definition' else 'name'
        key=(channel,value);tokens[entity].add(key);evidence[entity].add(fact['id'])
        if channel=='name':
            for token in value.replace('_',' ').split():
                if len(token)>=3:tokens[entity].add(('name_token',token))
    # Shared, already-supported neighbors are a retrieval clue, not identity proof.
    for edge in graph.get('edges',[]):
        if edge.get('status')!='accepted':continue
        if edge['source'] in nodes:tokens[edge['source']].add(('out_neighbor',(edge['relation'],edge['target'])))
        if edge['target'] in nodes:tokens[edge['target']].add(('in_neighbor',(edge['relation'],edge['source'])))
    for entity in sorted(nodes):
        for key in sorted(tokens[entity],key=canonical):
            if len(index[key])<bucket_limit:index[key].append(entity)
            else:omitted.add(entity)
    candidates={};truncated=0
    for entity in sorted(nodes):
        neighbors=defaultdict(set)
        for key in sorted(tokens[entity],key=canonical):
            for other in index[key]:
                if other!=entity:neighbors[other].add(key[0])
        ordered=sorted(neighbors,key=lambda other:(-len(neighbors[other]),other))
        if len(ordered)>per_entity:truncated+=len(ordered)-per_entity
        for other in ordered[:per_entity]:
            pair=tuple(sorted((entity,other)))
            if pair not in candidates and len(candidates)>=max_candidates:
                truncated+=1;continue
            channels[pair].update(neighbors[other])
            candidates[pair]={'members':list(pair),'evidence':sorted(evidence[entity]|evidence[other]),
                              'channels':sorted(channels[pair])}
    return list(candidates.values()),{'entities_checked':len(nodes),'candidate_pairs':len(candidates),
        'candidate_limit_omissions':truncated,'high_frequency_omitted_entities':len(omitted),
        'entity_limit_omissions':max(0,len(eligible)-len(nodes))}


def record_candidates(store, graph, scopes, domain='default', **limits):
    _initialize(store)
    candidates,counts=find_candidates(graph,**limits)
    scope_key=canonical(sorted(scopes))
    with store._transaction():
        for item in candidates:
            members=canonical(item['members'])
            old=store.connection.execute('SELECT candidate_id FROM identity_candidate WHERE domain=? AND scopes=? AND members=?',
                                         (domain,scope_key,members)).fetchone()
            candidate_id=old[0] if old else 'candidate_'+uuid.uuid4().hex
            store.connection.execute('INSERT OR REPLACE INTO identity_candidate VALUES(?,?,?,?,?,?)',
                (candidate_id,domain,scope_key,members,canonical(item['evidence']),canonical(item['channels'])))
    return counts


def _eligible_decisions(store, rows, scopes, domain):
    if not _exists(store):return []
    rows=[r for r in rows if r['status']=='accepted']
    facts={r['fact_id'] for r in rows}
    incident=_incident_facts(rows)
    result=[]
    for row in store.connection.execute('SELECT * FROM identity_decision WHERE active=1 AND domain=? ORDER BY revision',(domain,)):
        if not set(json.loads(row['scopes']))<=scopes:continue
        if not set(json.loads(row['evidence']))<=facts:continue
        if set(json.loads(row['evidence']))!=set().union(*(incident[e] for e in json.loads(row['members']))):continue
        if row['goal_revision']!=_goal_revision(store):continue
        if not _dependencies_current(store,json.loads(row['rule_dependencies'])):continue
        result.append(dict(row))
    mapping=_mapping(result)
    conflicting=set()
    for decision in result:
        if decision['verdict']!='different':continue
        seen=set()
        for entity in json.loads(decision['members']):
            canonical_id=mapping.get(entity,entity)
            if canonical_id in seen:conflicting.add(canonical_id)
            seen.add(canonical_id)
    if conflicting:
        result=[d for d in result if d['verdict']!='same' or not any(
            mapping.get(e,e) in conflicting for e in json.loads(d['members']))]
    # A new path can contradict a merge without touching either member's
    # incident edges. Validate the complete visible projection every generation.
    try:
        from lightgraph.rules.topology import validate_hierarchy_rows
        validate_hierarchy_rows(_project_rows(rows,_mapping(result),domain),rules=store.get_rules(include_stale=True))
    except LightGraphError:
        result=[d for d in result if d['verdict']!='same']
    return result


def _incident_facts(records):
    """Index original member attributes and incident edges, including new counterevidence."""
    index=defaultdict(set)
    for record in records:
        payload=record['payload']
        if isinstance(payload,str):payload=json.loads(payload)
        fid=record['fact_id'] if 'fact_id' in record.keys() else record['id']
        for name in ('entity_id','source','target'):
            if name in payload:index[payload[name]].add(fid)
    return index


def _goal_revision(store):
    exists=store.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='goal_revision'").fetchone()
    if not exists:return 0
    return store.connection.execute('SELECT COALESCE(MAX(revision),0) FROM goal_revision').fetchone()[0]


def _dependencies_current(store, dependencies):
    for rule_id,revision in dependencies:
        row=store.connection.execute('SELECT revision,dependency_state FROM rule WHERE rule_id=? ORDER BY revision DESC LIMIT 1',(rule_id,)).fetchone()
        if not row or row['revision']!=revision or row['dependency_state']!='valid':return False
    return True


def _mapping(decisions):
    groups={}
    for decision in decisions:
        if decision['verdict']!='same':continue
        members=json.loads(decision['members'])
        combined=set(members)
        for entity in members:combined.update(groups.get(entity,{entity}))
        for entity in combined:groups[entity]=combined
    return {entity:min(group) for entity,group in groups.items()}


def project_resolutions(store, rows, scopes, domain):
    """Authorize decisions and their original evidence before changing identities."""
    decisions=_eligible_decisions(store,rows,scopes,domain)
    mapping=_mapping(decisions)
    return _project_rows(rows,mapping,domain)


def _project_rows(rows,mapping,domain):
    if not mapping:return rows
    result=[]
    for source in rows:
        row=dict(source);payload=json.loads(row['payload']);changed=False
        for name in ('entity_id','source','target'):
            if payload.get(name) in mapping and mapping[payload[name]]!=payload[name]:
                payload[name]=mapping[payload[name]];changed=True
        if changed:
            if row['fact_kind']=='Edge' and payload['relation']=='same_entity' and payload['source']==payload['target']:
                continue
            if row['fact_kind']=='PropertyValue' and payload.get('name')=='label':
                payload['name']='alias'
            fact=GraphFact(isolation_domain=domain,fact_kind=row['fact_kind'],payload=payload)
            row['fact_id']=fact.fact_id;row['payload']=canonical(payload)
            # A projected support remains independently retractable by source.
            row['support_id']=digest('resolution_projection',row['support_id'],fact.fact_id)
        result.append(row)
    return result


def list_candidates(store, scopes, domain='default'):
    if not _exists(store):return {'revision':0,'candidates':[],'decisions':[]}
    graph=store.view(scopes,domain,include_candidates=True,apply_resolutions=False)
    nodes={n['id']:n for n in graph['nodes']};facts={f['id'] for f in graph['facts']}
    incident=_incident_facts([f for f in graph['facts'] if f['status']=='accepted'])
    rows=[r for r in store._fact_rows([h['publication_id'] for h in store.heads() if h['valid']])
          if r['isolation_domain']==domain and r['scope_ref'] in scopes]
    effective={d['decision_id'] for d in _eligible_decisions(store,rows,scopes,domain)}
    items=[];decisions=[]
    for row in store.connection.execute('SELECT * FROM identity_candidate WHERE domain=? ORDER BY candidate_id',(domain,)):
        members=json.loads(row['members'])
        if not set(json.loads(row['scopes']))<=scopes or not set(members)<=nodes.keys():continue
        evidence=json.loads(row['evidence'])
        items.append({'candidate_id':row['candidate_id'],'members':members,'channels':json.loads(row['channels']),
            'evidence':evidence,'status':'candidate' if set(evidence)<=facts else 'needs_review'})
        review=store.connection.execute('SELECT outcome FROM identity_review_attempt WHERE candidate_id=?',(row['candidate_id'],)).fetchone()
        if review:items[-1]['review_outcome']=review[0]
    for row in store.connection.execute('SELECT * FROM identity_decision WHERE domain=? ORDER BY revision',(domain,)):
        if not set(json.loads(row['scopes']))<=scopes:continue
        evidence=json.loads(row['evidence'])
        current=(row['decision_id'] in effective
                 and set(evidence)==set().union(*(incident[e] for e in json.loads(row['members'])))
                 and row['goal_revision']==_goal_revision(store)
                 and _dependencies_current(store,json.loads(row['rule_dependencies'])))
        decisions.append({'decision_id':row['decision_id'],'candidate_id':row['candidate_id'],
            'members':json.loads(row['members']),'verdict':row['verdict'],'origin':row['origin'],
            'revision':row['revision'],'active':bool(row['active']),
            'status':'active' if row['active'] and current else 'needs_review' if row['active'] else 'revoked'})
    revision=store.connection.execute('SELECT revision FROM resolution_version WHERE singleton=1').fetchone()[0]
    return {'revision':revision,'candidates':items,'decisions':decisions}


def decide(store, candidate_id, verdict, *, scopes, domain='default', expected_revision, members=None, origin='user',
           expected_heads=None, expected_evidence=None, expected_goal_revision=None):
    """Commit a whole-cluster decision against current evidence and version."""
    _initialize(store)
    if verdict not in ('same','different') or origin not in ('user','agent'):
        raise LightGraphError('invalid_identity_decision')
    with store._lock:
        # Obtain a raw authorized view before the write transaction; heads are
        # rechecked in that transaction, including unrelated graph changes.
        heads=store.heads()
        if expected_heads is not None and canonical(heads)!=canonical(expected_heads):
            raise LightGraphError('graph_generation_changed')
        graph=store.view(scopes,domain,include_candidates=False,apply_resolutions=False)
        candidate=store.connection.execute('SELECT * FROM identity_candidate WHERE candidate_id=?',(candidate_id,)).fetchone()
        if not candidate or candidate['domain']!=domain or not set(json.loads(candidate['scopes']))<=scopes:
            raise LightGraphError('identity_candidate_not_found')
        chosen=sorted(set(members or json.loads(candidate['members'])))
        if not set(json.loads(candidate['members']))<=set(chosen) or len(chosen)<2 or len(chosen)>64:
            raise LightGraphError('invalid_resolution_members')
        nodes={n['id']:n for n in graph['nodes'] if n['layer']=='business' and n['status']=='accepted'}
        if not set(chosen)<=nodes.keys():raise LightGraphError('resolution_requires_accepted_endpoints')
        facts={f['id']:f for f in graph['facts']}
        incident=_incident_facts(graph['facts'])
        evidence=sorted(set().union(*(incident[e] for e in chosen)))
        if expected_evidence is not None and evidence!=sorted(expected_evidence):
            raise LightGraphError('resolution_evidence_changed')
        if len(evidence)>4096:raise LightGraphError('resolution_evidence_limit')
        if not set(json.loads(candidate['evidence']))<=facts.keys():raise LightGraphError('resolution_evidence_changed')
        rows=store._fact_rows([h['publication_id'] for h in heads if h['valid']])
        rows=[r for r in rows if r['isolation_domain']==domain and r['scope_ref'] in scopes and r['status']=='accepted']
        prior=_eligible_decisions(store,rows,scopes,domain)
        known_rules={r.rule_id:r.revision for r in store.get_rules()}
        dependencies=sorted({(r['rule_id'],r['revision']) for r in rows if r['fact_id'] in evidence and r['rule_id'] in known_rules})
        mapping=_mapping(prior)
        closure=set(chosen)
        for entity,target in mapping.items():
            if target in {mapping.get(e,e) for e in chosen}:closure.add(entity)
        if verdict=='same':
            if closure!=set(chosen):raise LightGraphError('resolution_cluster_review_required')
            if len({nodes[e]['kind'] for e in chosen})>1:raise LightGraphError('resolution_type_conflict')
            for old in prior:
                if old['verdict']=='different' and len(set(json.loads(old['members']))&set(chosen))>1:
                    raise LightGraphError('resolution_cannot_link_conflict')
            # Aliases and positional roles can differ. Defining single values
            # cannot silently become a last-write-wins merged attribute.
            single_names={'definition','unit','formula','caliber','granularity'}
            single_names.update(name for name,definition in getattr(store.policy,'property_definitions',{}).items()
                                if not definition.multi_valued)
            single_names.difference_update({'label','alias','english_name','business_code','level','level_no','source_position'})
            for name in single_names:
                values={canonical(nodes[e]['properties'][name]) for e in chosen if name in nodes[e]['properties']}
                if len(values)>1:raise LightGraphError('resolution_attribute_conflict')
            for edge in graph['edges']:
                if edge['relation']=='parent_of' and {edge['source'],edge['target']}<=set(chosen):
                    raise LightGraphError('resolution_hierarchy_self_loop')
            projected_mapping=_mapping(prior+[{'verdict':'same','members':canonical(chosen)}])
            from lightgraph.rules.topology import validate_hierarchy_rows
            validate_hierarchy_rows(_project_rows(rows,projected_mapping,domain),rules=store.get_rules(include_stale=True))
        elif len({mapping.get(e,e) for e in chosen})<len(chosen):
            raise LightGraphError('revoke_same_decision_before_split')
        with store._transaction():
            revision=store.connection.execute('SELECT revision FROM resolution_version WHERE singleton=1').fetchone()[0]
            if revision!=expected_revision:raise LightGraphError('resolution_revision_conflict')
            if origin=='agent':
                human=store.connection.execute("SELECT scopes FROM identity_decision WHERE candidate_id=? AND domain=? AND active=1 AND origin='user'",
                                               (candidate_id,domain)).fetchall()
                if any(set(json.loads(r['scopes']))<=scopes for r in human):
                    raise LightGraphError('identity_human_decision_exists')
            if canonical(store.heads())!=canonical(heads):raise LightGraphError('graph_generation_changed')
            if expected_goal_revision is not None and _goal_revision(store)!=expected_goal_revision:
                raise LightGraphError('stale_goal_revision')
            decision_id='decision_'+uuid.uuid4().hex
            store.connection.execute('UPDATE identity_decision SET active=0 WHERE candidate_id=? AND domain=? AND scopes=?',
                (candidate_id,domain,canonical(sorted(scopes))))
            store.connection.execute('''INSERT INTO identity_decision
                (decision_id,candidate_id,domain,scopes,members,evidence,verdict,origin,revision,active,goal_revision,rule_dependencies)
                VALUES(?,?,?,?,?,?,?,?,?,1,?,?)''',
                (decision_id,candidate_id,domain,canonical(sorted(scopes)),canonical(chosen),canonical(evidence),verdict,origin,revision+1,
                 _goal_revision(store),canonical(dependencies)))
            store.connection.execute('UPDATE resolution_version SET revision=? WHERE singleton=1',(revision+1,))
        return {'decision_id':decision_id,'revision':revision+1,'verdict':verdict,'members':chosen}


def revoke(store, decision_id, *, scopes, domain='default', expected_revision):
    if not _exists(store):raise LightGraphError('identity_decision_not_found')
    with store._transaction():
        row=store.connection.execute('SELECT * FROM identity_decision WHERE decision_id=?',(decision_id,)).fetchone()
        if not row or row['domain']!=domain or not set(json.loads(row['scopes']))<=scopes:
            raise LightGraphError('identity_decision_not_found')
        revision=store.connection.execute('SELECT revision FROM resolution_version WHERE singleton=1').fetchone()[0]
        if revision!=expected_revision:raise LightGraphError('resolution_revision_conflict')
        store.connection.execute('UPDATE identity_decision SET active=0 WHERE decision_id=?',(decision_id,))
        store.connection.execute('UPDATE resolution_version SET revision=? WHERE singleton=1',(revision+1,))
    return {'revision':revision+1,'decision_id':decision_id,'active':False}


def review_with_agent(store, model, goal, corpus, *, scopes, domain='default', max_reviews=8):
    """Review bounded candidates from original facts, never from a presumed merge.

    A semantic judgment still passes whole-cluster, evidence, scope and graph
    checks. Human decisions are not silently overridden. Unchanged unresolved
    evidence is remembered as references so ordinary re-runs do not ask again.
    """
    counts={'reviewed':0,'accepted':0,'rejected':0,'unresolved':0,'deferred':0};issues=[]
    if not max_reviews or not callable(getattr(model,'complete',None)) or not getattr(model,'allow_source_samples',False):
        return counts,issues
    _initialize(store)
    from lightgraph.discovery.semantics import review_semantics,document_ref
    base_heads=store.heads();base_goal=_goal_revision(store)
    graph=store.view(scopes,domain,apply_resolutions=False)
    if canonical(base_heads)!=canonical(store.heads()):
        return {**counts,'deferred':1},[{'code':'graph_generation_changed'}]
    facts={f['id']:f for f in graph['facts']};incident=_incident_facts(graph['facts'])
    cached_listing=None;listing_stamp=None
    def current_listing():
        """Reuse only this call's unchanged read; every store write invalidates it.

        total_changes covers this connection, including review-attempt writes;
        data_version covers commits by other connections, including user votes.
        The caller also checks heads and goal on every candidate. A write during
        the listing read prevents reuse even if the captured result is returned.
        Commit-time evidence and whole-cluster checks remain authoritative.
        """
        nonlocal cached_listing,listing_stamp
        with store._lock:
            before=(store.connection.total_changes,
                    store.connection.execute('PRAGMA data_version').fetchone()[0])
            if cached_listing is not None and listing_stamp==before:
                return cached_listing
            cached_listing=list_candidates(store,scopes,domain)
            after=(store.connection.total_changes,
                   store.connection.execute('PRAGMA data_version').fetchone()[0])
            listing_stamp=before if before==after else None
            return cached_listing
    listing=current_listing()
    known_rules={r.rule_id:r for r in store.get_rules(scopes=scopes,domain=domain)}
    reviewed_groups=set()
    exhausted=False
    for candidate in listing['candidates']:
        if canonical(base_heads)!=canonical(store.heads()) or base_goal!=_goal_revision(store):
            counts['deferred']+=1;issues.append({'code':'graph_generation_changed'});break
        # Earlier decisions in this loop can have enlarged either endpoint's
        # group. Review all original members and their evidence together; a
        # pairwise approval cannot authorize a transitive whole-group merge.
        current=current_listing()
        effective=[d for d in current['decisions'] if d['active'] and d['status']=='active']
        mapping=_mapping([{**d,'members':canonical(d['members'])} for d in effective])
        targets={mapping.get(e,e) for e in candidate['members']}
        if len(targets)==1:continue
        members=sorted(set(candidate['members']) | {e for e,target in mapping.items() if target in targets})
        prior=[d for d in current['decisions'] if d['candidate_id']==candidate['candidate_id'] and d['active']]
        if any(d['origin']=='user' or d['status']=='active' for d in prior):continue
        group_key=tuple(members)
        if group_key in reviewed_groups:continue
        reviewed_groups.add(group_key)
        relevant=set().union(*(incident[e] for e in members))
        docs={};source_rules={s['rule_id'] for s in graph['supports'] if s['fact_id'] in relevant}
        for rid in sorted(source_rules):
            rule=known_rules.get(rid)
            if rule:
                for document in corpus.context_for(rule.source):docs[document_ref(document)]=document
        dependencies=canonical(['whole_cluster_review/2',_goal_revision(store),members,sorted(relevant),sorted(docs),
            sorted((d['decision_id'],d['revision']) for d in effective if set(d['members']) & set(members)),
            sorted((rid,known_rules[rid].revision,known_rules[rid].dependencies.schema_ref)
                   for rid in source_rules if rid in known_rules)])
        previous=store.connection.execute('SELECT * FROM identity_review_attempt WHERE candidate_id=?',(candidate['candidate_id'],)).fetchone()
        if previous and previous['dependencies']==dependencies and previous['outcome']!='deferred':continue
        if exhausted or counts['reviewed']>=max_reviews:
            counts['deferred']+=1;continue
        outcome='unresolved'
        try:
            # An exhausted shared model allowance applies to the entire queue,
            # not one candidate. Do not count blocked calls as completed reviews.
            allowance=getattr(model,'budget',None)
            if allowance is not None:
                from lightgraph.discovery.budget import Cost
                allowance.admit(Cost(llm_calls=1,llm_tokens=1))
            if len(members)>64 or len(relevant)>64:raise LightGraphError('identity_review_evidence_limit')
            # These are approved graph facts, not source samples. The registry of
            # references prevents a reviewer inventing unseen evidence.
            selected=[facts[fid] for fid in sorted(relevant)]
            packet={'goal':goal.model_dump(mode='json'),'scope':None,
                'authorized_entities':members,
                'graph_facts':[{'reference':'graph_fact_'+f['id'],'fact_kind':f['fact_kind'],'payload':f['payload']} for f in selected],
                'probe_digest':[{'probe_id':'graph_fact_'+f['id'],'operator':'published_graph_fact','aggregates':{}} for f in selected],
                'iteration_context':{'goal_revision':_goal_revision(store),'stage':'graph_reconcile'},
                'subtask':'Decide whether ALL authorized occurrences denote the same business object, including every member of previously accepted groups. Roles and codes may differ. Names alone are insufficient.'}
            proposition={'predicate':'same_entity','source_entity':candidate['members'][0],
                         'target_entity':candidate['members'][1]}
            if len(members)>2:proposition['members']=members
            counts['reviewed']+=1
            review_heads=store.heads()
            review_revision=current_listing()['revision']
            review_goal=_goal_revision(store)
            refs=review_semantics(model,packet,proposition,list(docs.values()))
            meaningful={'graph_fact_'+f['id'] for f in selected if f['fact_kind']=='Edge' or (
                f['fact_kind']=='PropertyValue' and f['payload']['name'] not in ('label','alias','english_name'))}
            meaningful.update(docs)
            if not meaningful.intersection(refs):raise LightGraphError('identity_review_only_names')
            decide(store,candidate['candidate_id'],'same',scopes=scopes,domain=domain,
                   expected_revision=review_revision,members=members,origin='agent',expected_heads=review_heads,
                   expected_evidence=relevant,expected_goal_revision=review_goal)
            counts['accepted']+=1;outcome='accepted'
        except LightGraphError as exc:
            if exc.code in ('wall_time_budget_exhausted','llm_calls_budget_exhausted','llm_tokens_budget_exhausted'):
                exhausted=True
            deferred=(exc.code in {'graph_generation_changed','resolution_revision_conflict',
                'resolution_evidence_changed','resolution_cluster_review_required','stale_goal_revision'}
                or any(part in exc.code for part in ('budget','timeout','limit','too_large','model_')))
            rejected=exc.code=='semantic_claim_refuted'
            # Reject this proposed merge, without asserting every member differs
            # or creating a cannot-link decision from an early negative review.
            counts['rejected' if rejected else 'deferred' if deferred else 'unresolved']+=1
            outcome='deferred' if deferred else exc.code
            issues.append({'code':exc.code,'count':1,**({'status':'rejected'} if rejected else {})})
        with store._transaction():
            store.connection.execute('INSERT OR REPLACE INTO identity_review_attempt VALUES(?,?,?)',
                                     (candidate['candidate_id'],dependencies,outcome))
    return counts,issues
