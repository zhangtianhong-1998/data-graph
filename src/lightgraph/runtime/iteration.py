"""Durable work references between ephemeral Agent sessions; never source values."""
from __future__ import annotations

import json
import uuid
from typing import Literal
from pydantic import Field
from lightgraph.contracts import ExtractionGoal, RuleSpec, SourceRef, StrictModel, canonical
from lightgraph.errors import LightGraphError

REVIEWABLE_CODES = frozenset({
    'fd_violation', 'null_key', 'identity_definition_conflict',
    'shared_identity_attribute_conflict', 'shared_attribute_conflict',
    'reference_unmatched', 'reference_ambiguous', 'unmatched_reference',
    'ambiguous_reference', 'reference_partial_null', 'reference_null_endpoint', 'hierarchy_cycle', 'multiple_parents',
    'new_discriminator_partition', 'discriminator_cardinality_drift',
    'same_code_label_conflict', 'unexplained_role_occurrence',
    'same_code_diff_label', 'cross_level_code_reuse', 'non_contiguous_level_code',
    'fill_mismatch', 'reference_missing', 'multiple_parent', 'hierarchy_self_loop',
    'reference_self_link_forbidden', 'reference_self_link_semantics_unreviewed',
    'reference_arity_mismatch',
    'identity_domain_attribute_conflict', 'identity_migration_conflict',
    'namespace_scope_missing',
})
WorkState = Literal['pending', 'running', 'resolved', 'unresolved', 'deferred', 'superseded']


def _safe_draft_diagnostics(issues, table, contract_codes):
    """Retain schema paths and counts, never input keys, IDs or allowed values."""
    from lightgraph.discovery.models import ProposalBatch
    from lightgraph.discovery.hierarchy_plan import HierarchyPlan
    from lightgraph.discovery.proposal_session import DiscoveryFinish
    from lightgraph.semantic_schema import SemanticSchema
    from lightgraph.discovery.validation_feedback import _property_names, _path, _JSON_CONSTRAINTS
    codes=set(contract_codes)|{'invalid_tool_arguments','invalid_proposal_arguments',
        'dangling_evidence_reference','unauthorized_column_reference','unauthorized_source_reference'}
    names=set().union(*(_property_names(model.model_json_schema())
        for model in (ProposalBatch,HierarchyPlan,SemanticSchema,DiscoveryFinish)))
    records=[]
    for issue in issues:
        if (issue.get('table')!=table or issue.get('code')!='discovery_draft_field_diagnostic'
                or issue.get('reason_code') not in codes
                or issue.get('constraint') not in _JSON_CONSTRAINTS|{'reference','contract','semantic_contract'}):
            continue
        try:path=json.loads(issue.get('field_path',''))
        except (ValueError,TypeError):continue
        if not isinstance(path,list):continue
        count=issue.get('count',1)
        if type(count) is not int or count<=0:continue
        records.append({'code':issue['reason_code'],'path':_path(path,names),
                        'constraint':issue['constraint'],'count':min(count,1000000)})
        if len(records)>=64:break
    return records


class WorkItem(StrictModel):
    work_id: str
    goal_revision: int
    source: SourceRef
    task: Literal['metadata_discovery', 'relation_discovery', 'coverage_discovery', 'review_pattern']
    pattern_codes: list[str] = Field(default_factory=list)
    rule_revisions: dict[str, int] = Field(default_factory=dict)
    context_version: str
    state: WorkState = 'pending'
    attempts: int = 0
    observation_counts: dict[str, int] = Field(default_factory=dict)
    column_refs: list[str] = Field(default_factory=list)


def rule_shape(rule: RuleSpec) -> str:
    """Compare executable interpretations in memory, excluding bookkeeping."""
    value = rule.model_dump(mode='json', by_alias=True)
    for name in ('revision', 'status', 'validation_ref', 'coverage', 'evidence'):
        value.pop(name, None)
    return canonical(value)


class IterationLedger:
    def __init__(self, store, goal: ExtractionGoal, scopes: list[str]):
        self.store, self.connection = store, store.connection
        self.candidate_queue = None
        self.connection.executescript('''
        CREATE TABLE IF NOT EXISTS discovery_feedback (
          goal_revision INTEGER NOT NULL, source_ref TEXT NOT NULL, task TEXT NOT NULL, codes TEXT NOT NULL,
          PRIMARY KEY(goal_revision,source_ref,task));
        CREATE TABLE IF NOT EXISTS discovery_diagnostic (
          goal_revision INTEGER NOT NULL, source_ref TEXT NOT NULL, task TEXT NOT NULL, diagnostics TEXT NOT NULL,
          PRIMARY KEY(goal_revision,source_ref,task));
        CREATE TABLE IF NOT EXISTS goal_revision (
          revision INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS iteration_work (
          work_id TEXT PRIMARY KEY, goal_revision INTEGER NOT NULL,
          source_ref TEXT NOT NULL, task TEXT NOT NULL, context_version TEXT NOT NULL,
          pattern_codes TEXT NOT NULL, rule_revisions TEXT NOT NULL,
          state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS iteration_work_lookup
          ON iteration_work(goal_revision,source_ref,task,context_version);
        ''')
        columns={r[1] for r in self.connection.execute('PRAGMA table_info(iteration_work)')}
        if 'observation_counts' not in columns:
            self.connection.execute("ALTER TABLE iteration_work ADD COLUMN observation_counts TEXT NOT NULL DEFAULT '{}' ")
        if 'column_refs' not in columns:
            self.connection.execute("ALTER TABLE iteration_work ADD COLUMN column_refs TEXT NOT NULL DEFAULT '[]'")
        payload = canonical({'goal': goal.model_dump(mode='json'), 'scopes': sorted(scopes)})
        with store._transaction():
            latest = self.connection.execute(
                'SELECT revision,payload FROM goal_revision ORDER BY revision DESC LIMIT 1').fetchone()
            self.goal_revision = latest['revision'] if latest and latest['payload'] == payload else self.connection.execute(
                'INSERT INTO goal_revision(payload) VALUES(?)', (payload,)).lastrowid
            # A crash is not a semantic decision. Re-query complete source units.
            self.connection.execute("UPDATE iteration_work SET state='deferred' WHERE state='running'")

    @staticmethod
    def _item(row) -> WorkItem:
        return WorkItem(work_id=row['work_id'], goal_revision=row['goal_revision'],
            source=SourceRef.model_validate(json.loads(row['source_ref'])), task=row['task'],
            context_version=row['context_version'], pattern_codes=json.loads(row['pattern_codes']),
            rule_revisions=json.loads(row['rule_revisions']), state=row['state'], attempts=row['attempts'],
            observation_counts=json.loads(row['observation_counts']),column_refs=json.loads(row['column_refs']))

    def enqueue(self, source: SourceRef, *, task='review_pattern', codes=(),
                rules=(), context_version: str, force=False, observation_counts=None, column_refs=()) -> WorkItem:
        codes = sorted(set(codes))
        if set(codes) - REVIEWABLE_CODES:
            raise LightGraphError('unregistered_iteration_pattern')
        counts=dict(observation_counts or {})
        if set(counts)-{'distinct_tuples','conflicts'} or any(type(v) is not int or v<0 for v in counts.values()):
            raise LightGraphError('invalid_iteration_aggregates')
        revisions = {r.rule_id: r.revision for r in rules}
        column_refs=sorted(set(column_refs))
        for ref in [context_version, *revisions, *column_refs]:
            self.store.policy._reference(ref)
        source_ref = canonical(source.model_dump(mode='json', by_alias=True))
        with self.store._transaction():
            rows = self.connection.execute('''SELECT * FROM iteration_work
                WHERE goal_revision=? AND source_ref=? AND task=? AND context_version=?
                ORDER BY rowid DESC''', (self.goal_revision, source_ref, task, context_version)).fetchall()
            for row in rows:
                if (json.loads(row['pattern_codes']) == codes and json.loads(row['rule_revisions']) == revisions
                    and json.loads(row['observation_counts'])==counts and json.loads(row['column_refs'])==column_refs):
                    item = self._item(row)
                    if force or item.state in ('deferred','superseded') or (item.state == 'resolved' and task == 'review_pattern'):
                        self.connection.execute("UPDATE iteration_work SET state='pending' WHERE work_id=?", (item.work_id,))
                        item.state = 'pending'
                    return item
            item = WorkItem(work_id='work_' + uuid.uuid4().hex, goal_revision=self.goal_revision,
                source=source, task=task, pattern_codes=codes, rule_revisions=revisions, context_version=context_version,
                observation_counts=counts,column_refs=column_refs)
            self.connection.execute('INSERT INTO iteration_work(work_id,goal_revision,source_ref,task,context_version,pattern_codes,rule_revisions,state,attempts,observation_counts,column_refs) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (item.work_id, self.goal_revision, source_ref, task, context_version,
                 canonical(codes), canonical(revisions), item.state, item.attempts,canonical(counts),canonical(column_refs)))
            return item

    def transition(self, item: WorkItem, state: WorkState):
        if state not in ('running', 'resolved', 'unresolved', 'deferred', 'superseded'):
            raise LightGraphError('invalid_work_transition')
        if item.goal_revision != self.goal_revision:
            raise LightGraphError('stale_goal_revision')
        if state == 'running':
            if item.state not in ('pending', 'deferred'):
                raise LightGraphError('work_not_pending')
            item.attempts += 1
        item.state = state
        with self.store._transaction():
            self.connection.execute('UPDATE iteration_work SET state=?,attempts=? WHERE work_id=?',
                                    (state, item.attempts, item.work_id))

    def supersede_previous(self, item: WorkItem):
        """An endpoint/coverage revision replaces older exploration tasks."""
        with self.store._transaction():
            self.connection.execute("""UPDATE iteration_work SET state='superseded'
                WHERE goal_revision=? AND source_ref=? AND task=? AND work_id!=?
                AND state!='superseded' """, (item.goal_revision,
                canonical(item.source.model_dump(mode='json',by_alias=True)),item.task,item.work_id))

    def resolve_exploration(self, source: SourceRef, task: str):
        with self.store._transaction():
            self.connection.execute("""UPDATE iteration_work SET state='resolved'
                WHERE goal_revision=? AND source_ref=? AND task=? AND state!='superseded'""",
                (self.goal_revision,canonical(source.model_dump(mode='json',by_alias=True)),task))

    def resolve_table(self, source: SourceRef):
        """Successful full validation resolves prior failures without model calls."""
        with self.store._transaction():
            self.connection.execute('''UPDATE iteration_work SET state='resolved'
                WHERE goal_revision=? AND source_ref=? AND task='review_pattern'
                AND pattern_codes NOT LIKE '%unexplained_role_occurrence%'
                AND pattern_codes NOT LIKE '%namespace_scope_missing%' ''',
                (self.goal_revision, canonical(source.model_dump(mode='json', by_alias=True))))

    def unresolved_tables(self, source_id: str, contexts: dict[str, str]) -> set[str]:
        return {i.source.schema_name + '.' + i.source.table for i in self.items()
            if i.source.source_id == source_id and i.task == 'review_pattern' and i.state == 'unresolved'
            and contexts.get(i.source.schema_name + '.' + i.source.table) == i.context_version}

    def items(self) -> list[WorkItem]:
        return [self._item(row) for row in self.connection.execute(
            'SELECT * FROM iteration_work WHERE goal_revision=? ORDER BY rowid', (self.goal_revision,))]

    def record_discovery_feedback(self, sources, issues, *, task):
        """Persist only fixed failure categories, never rejected proposal bodies."""
        if task not in ('metadata_discovery','relation_discovery','coverage_discovery','review_pattern'):
            raise LightGraphError('unregistered_discovery_feedback_task')
        from lightgraph.discovery.repair import REPAIRABLE_PROPOSAL_CODES, SEMANTIC_REVIEW_PROTOCOL_CODES
        allowed=set(REPAIRABLE_PROPOSAL_CODES)|set(SEMANTIC_REVIEW_PROTOCOL_CODES)|{
            'semantic_claim_unresolved','semantic_claim_refuted','semantic_condition_unresolved',
            'discovery_invalid_drafts','discovery_submission_incomplete','discovery_submission_pending',
            'no_confirmable_entity_group','model_abstained','model_response_truncated'}
        allowed.add('namespace_scope_missing')
        def feedback_code(issue):
            # Submission diagnostics wrap a fixed reason code; never persist
            # the rejected payload, permitted IDs or arbitrary field values.
            if issue.get('code') in {'discovery_draft_rejected','discovery_draft_field_diagnostic'}:
                return issue.get('reason_code')
            return issue.get('code')
        with self.store._transaction():
            for source in sources:
                name=source.schema_name+'.'+source.table
                codes=sorted({feedback_code(i) for i in issues if feedback_code(i) in allowed
                              and i.get('table',name)==name})[:32]
                self.connection.execute('INSERT OR REPLACE INTO discovery_feedback VALUES(?,?,?,?)',
                    (self.goal_revision,canonical(source.model_dump(mode='json',by_alias=True)),task,canonical(codes)))
                diagnostics=_safe_draft_diagnostics(issues,name,allowed)
                self.connection.execute('INSERT OR REPLACE INTO discovery_diagnostic VALUES(?,?,?,?)',
                    (self.goal_revision,canonical(source.model_dump(mode='json',by_alias=True)),task,canonical(diagnostics)))

    def context(self, items, rules, *, iteration: int) -> dict:
        selected = {(i.source.source_id, i.source.schema_name, i.source.table) for i in items}
        relevant = [r for r in rules if (r.source.source_id, r.source.schema_name, r.source.table) in selected]
        # Mappings only: no observations or query filter values in "knowledge".
        known = [{'rule_id': r.rule_id, 'revision': r.revision, 'kind': r.kind,
                  'source': r.source.model_dump(mode='json', by_alias=True),
                  'entity_kind': r.identity.entity_kind, 'key_columns': r.identity.key_columns,
                  'attributes': r.attributes.model_dump(mode='json'),
                  'level_policy': r.identity.level_policy,
                  'namespace_columns': list(r.scope.namespaces),
                  'identity_contract': r.identity.model_dump(mode='json'),
                  'hierarchy_contract': r.hierarchy.model_dump(mode='json') if r.hierarchy else None,
                  'hierarchy_levels': [level.model_dump(mode='json') for level in r.hierarchy.levels]
                      if r.hierarchy else []} for r in relevant[:24]]
        failures=[]
        for source_id,schema,table in sorted(selected):
            source=SourceRef(source_id=source_id,schema=schema,table=table)
            rows=self.connection.execute('SELECT task,codes FROM discovery_feedback WHERE goal_revision=? AND source_ref=? ORDER BY task',
                (self.goal_revision,canonical(source.model_dump(mode='json',by_alias=True)))).fetchall()
            for task,codes in rows:
                row=self.connection.execute('SELECT diagnostics FROM discovery_diagnostic WHERE goal_revision=? AND source_ref=? AND task=?',
                    (self.goal_revision,canonical(source.model_dump(mode='json',by_alias=True)),task)).fetchone()
                diagnostics=json.loads(row['diagnostics']) if row else []
                if json.loads(codes) or diagnostics:
                    failures.append({'source':source.model_dump(mode='json',by_alias=True),
                                     'task':task,'codes':json.loads(codes),
                                     **({'diagnostics':diagnostics} if diagnostics else {})})
        return {'goal_revision': self.goal_revision, 'iteration': iteration,
                'pending_candidates':self.candidate_queue.context(selected) if self.candidate_queue else [],
                'prior_discovery_feedback':failures,
                'work_items': [i.model_dump(mode='json', by_alias=True) for i in items[:32]],
                'known_rules': known, 'known_rules_total': len(relevant),
                'knowledge_truncated': len(relevant) > len(known)}

    def counts(self) -> dict[str, int]:
        result = {f'work_{s}': 0 for s in ('pending', 'running', 'resolved', 'unresolved', 'deferred', 'superseded')}
        for item in self.items():
            result['work_' + item.state] += 1
        return result
