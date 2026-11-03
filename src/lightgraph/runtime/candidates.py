"""Durable discovery work, containing issued references rather than source values.

A human can dismiss or request another investigation. Neither operation supplies
semantic evidence or publishes a rule. Publication remains with the rule engine.
"""
from __future__ import annotations

import json
from lightgraph.contracts import canonical, digest
from lightgraph.errors import LightGraphError


class CandidateQueue:
    def __init__(self, store, goal, scopes, domain, sources=()):
        self.store=store; self.connection=store.connection
        self.scope_key=digest(goal.model_dump(mode='json'),sorted(scopes),domain,
            [s.model_dump(mode='json',by_alias=True) for s in sources])
        self.connection.executescript('''
        CREATE TABLE IF NOT EXISTS discovery_candidate (
          scope_key TEXT NOT NULL,candidate_id TEXT NOT NULL,payload TEXT NOT NULL,
          state TEXT NOT NULL,revision INTEGER NOT NULL,
          PRIMARY KEY(scope_key,candidate_id));
        CREATE TABLE IF NOT EXISTS discovery_candidate_event (
          scope_key TEXT NOT NULL,candidate_id TEXT NOT NULL,revision INTEGER NOT NULL,
          action TEXT NOT NULL,PRIMARY KEY(scope_key,candidate_id,revision));
        CREATE TABLE IF NOT EXISTS discovery_candidate_replacement (
          scope_key TEXT NOT NULL,candidate_id TEXT NOT NULL,rule_revisions TEXT NOT NULL,
          PRIMARY KEY(scope_key,candidate_id));
        ''')
        # Older runs retained closed reference-contract violations as pending.
        # Reclassify only that exact, safe failure family; semantic uncertainty
        # and interrupted reviews remain work requiring investigation.
        from lightgraph.discovery.repair import REJECTED_REFERENCE_CONTRACT_CODES
        with self.store._transaction():
            rows=self.connection.execute("SELECT * FROM discovery_candidate WHERE scope_key=? AND state='pending'",
                (self.scope_key,)).fetchall()
            for row in rows:
                value=json.loads(row['payload'])
                latest=self.connection.execute('SELECT action FROM discovery_candidate_event '
                    'WHERE scope_key=? AND candidate_id=? ORDER BY revision DESC LIMIT 1',
                    (self.scope_key,row['candidate_id'])).fetchone()
                if latest and latest['action']=='retry_started':
                    continue
                if (value.get('proposal_kind')=='reference'
                        and value.get('failure_code')=='reference_contract_unresolved'
                        and value.get('failure_details')
                        and set(value['failure_details']) <= REJECTED_REFERENCE_CONTRACT_CODES):
                    value['status']='rejected'
                    self.connection.execute('UPDATE discovery_candidate SET payload=? WHERE scope_key=? AND candidate_id=?',
                        (canonical(value),self.scope_key,row['candidate_id']))
                    self._state(row,'rejected','contract_rejected')

    def record(self, result):
        from lightgraph.discovery.models import PendingCandidate
        with self.store._transaction():
            for value in result.pending_candidates:
                candidate=PendingCandidate.model_validate(value.model_dump(mode='json'))
                payload=canonical(candidate.model_dump(mode='json',by_alias=True))
                row=self.connection.execute('SELECT payload,state,revision FROM discovery_candidate WHERE scope_key=? AND candidate_id=?',
                    (self.scope_key,candidate.candidate_id)).fetchone()
                if row and candidate.failure_code=='semantic_review_interrupted':
                    old=json.loads(row['payload'])
                    if (old.get('failure_code')!='semantic_review_interrupted'
                            and old.get('dependency_fingerprint')==candidate.dependency_fingerprint
                            and old.get('document_refs',[])==candidate.document_refs):
                        # A failed request supplies no new judgment and cannot
                        # erase a prior verdict or its counterevidence.
                        continue
                if row and row['state']=='rejected' and candidate.status!='rejected':
                    old=json.loads(row['payload'])
                    changed=(old.get('dependency_fingerprint')!=candidate.dependency_fingerprint
                        or old.get('document_refs',[])!=candidate.document_refs)
                    if not changed:
                        continue  # Same claim/materials cannot reopen a terminal rejection.
                if row and row['payload']==payload:
                    if candidate.status=='rejected' and row['state'] not in ('rejected','dismissed'):
                        self._state(self._find(candidate.candidate_id),'rejected','contract_rejected')
                    elif row['state']=='resolved':
                        self._state(self._find(candidate.candidate_id),'pending','conflict_reobserved')
                        self.connection.execute('DELETE FROM discovery_candidate_replacement WHERE scope_key=? AND candidate_id=?',
                            (self.scope_key,candidate.candidate_id))
                    continue
                revision=row['revision']+1 if row else 1
                self.connection.execute('INSERT OR REPLACE INTO discovery_candidate VALUES(?,?,?,?,?)',
                    (self.scope_key,candidate.candidate_id,payload,candidate.status,revision))
                self.connection.execute('DELETE FROM discovery_candidate_replacement WHERE scope_key=? AND candidate_id=?',
                    (self.scope_key,candidate.candidate_id))
                self._event(candidate.candidate_id,revision,
                    'contract_rejected' if candidate.status=='rejected' else 'observed')
            for candidate_id in result.resolved_candidate_ids:
                self.connection.execute('DELETE FROM discovery_candidate_replacement WHERE scope_key=? AND candidate_id=?',
                    (self.scope_key,candidate_id))
                row=self._find(candidate_id,required=False)
                if row and row['state'] not in ('resolved','revised','rejected'):
                    self._state(row,'revised','replacement_proposed')
            rules={r.rule_id:r.revision for r in result.rules}
            for candidate_id,rule_ids in getattr(result,'candidate_replacement_rules',{}).items():
                if self._find(candidate_id,required=False) and rule_ids and set(rule_ids)<=set(rules):
                    self.connection.execute('INSERT OR REPLACE INTO discovery_candidate_replacement VALUES(?,?,?)',
                        (self.scope_key,candidate_id,canonical({rid:rules[rid] for rid in rule_ids})))

    def confirm_publications(self,published):
        """A semantic repair is resolved only after every replacement publishes."""
        heads={}
        for head in self.store.heads():heads.setdefault(head['logical_rule_id'],[]).append(head)
        revisions={}
        for rule_id in set(published):
            current=[head for head in heads.get(rule_id,[]) if head['valid']]
            # One updated partition is not a completed replacement. Every valid
            # head for the rule must be a published complete generation.
            if not current:
                continue
            publications=[]
            complete=True
            for head in current:
                publication=self.store._publication(head['publication_id'])
                if publication['state']!='published' or not self.store._is_rule_generation(publication['publication_id']):
                    complete=False
                    break
                publications.append(publication)
            if complete and publications:
                revisions[rule_id]=max(item['revision'] for item in publications)
        with self.store._transaction():
            for row in self.connection.execute('SELECT candidate_id,rule_revisions FROM discovery_candidate_replacement WHERE scope_key=?',(self.scope_key,)).fetchall():
                expected=json.loads(row['rule_revisions'])
                candidate=self._find(row['candidate_id'])
                if expected and all(revisions.get(rid)==rev for rid,rev in expected.items()) and candidate['state']=='revised':
                    self._state(candidate,'resolved','replacement_published')

    def _event(self, candidate_id, revision, action):
        self.connection.execute('INSERT INTO discovery_candidate_event VALUES(?,?,?,?)',
            (self.scope_key,candidate_id,revision,action))

    def _find(self, candidate_id, *, required=True):
        row=self.connection.execute('SELECT * FROM discovery_candidate WHERE scope_key=? AND candidate_id=?',
            (self.scope_key,candidate_id)).fetchone()
        if row is None and required:raise LightGraphError('discovery_candidate_not_found')
        return row

    def _state(self,row,state,action):
        revision=row['revision']+1
        self.connection.execute('UPDATE discovery_candidate SET state=?,revision=? WHERE scope_key=? AND candidate_id=?',
            (state,revision,self.scope_key,row['candidate_id']))
        self._event(row['candidate_id'],revision,action)
        return revision

    def decide(self,candidate_id,action,expected_revision):
        if action not in ('dismiss','retry','reopen'):
            raise LightGraphError('discovery_candidate_action_invalid')
        with self.store._transaction():
            row=self._find(candidate_id)
            if type(expected_revision) is not int or expected_revision!=row['revision']:
                raise LightGraphError('discovery_candidate_revision_conflict')
            if row['state']=='resolved':raise LightGraphError('discovery_candidate_already_resolved')
            if row['state']=='rejected' and action!='retry':
                raise LightGraphError('discovery_candidate_closed')
            state={'dismiss':'dismissed','retry':'retry_requested','reopen':'pending'}[action]
            revision=self._state(row,state,action)
        return {'candidate_id':candidate_id,'state':state,'revision':revision,'published':False}

    def listing(self,offset=0,limit=50):
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100:
            raise LightGraphError('discovery_candidate_page_invalid')
        total=self.connection.execute('SELECT count(*) FROM discovery_candidate WHERE scope_key=?',(self.scope_key,)).fetchone()[0]
        rows=self.connection.execute('SELECT * FROM discovery_candidate WHERE scope_key=? ORDER BY candidate_id LIMIT ? OFFSET ?',
            (self.scope_key,limit,offset)).fetchall()
        values=[]
        for row in rows:
            history=self.connection.execute('SELECT revision,action FROM discovery_candidate_event WHERE scope_key=? AND candidate_id=? ORDER BY revision DESC LIMIT 20',
                (self.scope_key,row['candidate_id'])).fetchall()
            values.append({**json.loads(row['payload']),'state':row['state'],'revision':row['revision'],
                'history':[dict(event) for event in history]})
        return {'candidates':values,'total_candidates':total,'next_offset':offset+limit if offset+limit<total else None,
                'counts':self.counts()}

    def counts(self):
        return {row['state']:row['n'] for row in self.connection.execute(
            'SELECT state,count(*) n FROM discovery_candidate WHERE scope_key=? GROUP BY state',(self.scope_key,))}

    def context(self, selected):
        result=[]
        for row in self.connection.execute("SELECT payload FROM discovery_candidate WHERE scope_key=? AND state IN ('pending','retry_requested','revised') ORDER BY candidate_id",(self.scope_key,)):
            value=json.loads(row[0]);source=value['source']
            # A requested investigation is fresh work, not publication authority
            # for the rejected proposal stored in its history.
            value['status']='pending'
            if (source['source_id'],source.get('schema','public'),source['table']) in selected:
                result.append(value)
            if len(result)>=16:break
        return result

    def refresh_context(self, dependencies):
        """Schema/document changes retire stored mappings, never replay them."""
        with self.store._transaction():
            rows=self.connection.execute("SELECT * FROM discovery_candidate WHERE scope_key=? "
                "AND state IN ('pending','retry_requested','revised','rejected')",(self.scope_key,)).fetchall()
            for row in rows:
                value=json.loads(row['payload']);source=value['source']
                key=(source['source_id'],source.get('schema','public'),source['table'])
                expected=value.get('dependency_fingerprint')
                if expected and (key not in dependencies or dependencies[key]!=expected):
                    self._state(row,'stale','dependencies_changed')

    def retry_sources(self):
        from lightgraph.contracts import SourceRef
        return [SourceRef.model_validate(json.loads(row[0])['source']) for row in self.connection.execute(
            "SELECT payload FROM discovery_candidate WHERE scope_key=? AND state='retry_requested'",(self.scope_key,))]

    def consume_retries(self, source_id, tables):
        """One explicit user request schedules one attempt, even if it fails again."""
        with self.store._transaction():
            rows=self.connection.execute("SELECT * FROM discovery_candidate WHERE scope_key=? AND state='retry_requested'",(self.scope_key,)).fetchall()
            for row in rows:
                source=json.loads(row['payload'])['source']
                if source['source_id']==source_id and source.get('schema','public')+'.'+source['table'] in tables:
                    self._state(row,'pending','retry_started')

    def finish_retries(self, source_id, tables):
        """Close completed investigations which did not replace a rejected claim.

        The scheduler calls this only for a finished relation investigation,
        including an explicit abstention. An interrupted task must leave the
        retry pending. A newly observed semantic gap has its own pending payload
        and is not overwritten by this retirement of the original rejection.
        """
        completed=0
        with self.store._transaction():
            rows=self.connection.execute("SELECT * FROM discovery_candidate WHERE scope_key=? AND state='pending'",
                (self.scope_key,)).fetchall()
            for row in rows:
                value=json.loads(row['payload']);source=value['source']
                if (value.get('status')!='rejected' or value.get('proposal_kind')!='reference'
                        or source['source_id']!=source_id
                        or source.get('schema','public')+'.'+source['table'] not in tables):
                    continue
                latest=self.connection.execute('SELECT action FROM discovery_candidate_event '
                    'WHERE scope_key=? AND candidate_id=? ORDER BY revision DESC LIMIT 1',
                    (self.scope_key,row['candidate_id'])).fetchone()
                if latest and latest['action']=='retry_started':
                    self._state(row,'rejected','retry_completed_without_replacement')
                    completed+=1
        return completed
