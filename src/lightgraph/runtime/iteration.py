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
    'ambiguous_reference', 'hierarchy_cycle', 'multiple_parents',
    'new_discriminator_partition', 'discriminator_cardinality_drift',
    'same_code_label_conflict', 'unexplained_role_occurrence',
    'same_code_diff_label', 'cross_level_code_reuse', 'non_contiguous_level_code',
    'fill_mismatch', 'reference_missing', 'multiple_parent', 'hierarchy_self_loop',
    'identity_domain_attribute_conflict', 'identity_migration_conflict',
})
WorkState = Literal['pending', 'running', 'resolved', 'unresolved', 'deferred']


class WorkItem(StrictModel):
    work_id: str
    goal_revision: int
    source: SourceRef
    task: Literal['metadata_discovery', 'review_pattern']
    pattern_codes: list[str] = Field(default_factory=list)
    rule_revisions: dict[str, int] = Field(default_factory=dict)
    context_version: str
    state: WorkState = 'pending'
    attempts: int = 0
    observation_counts: dict[str, int] = Field(default_factory=dict)


def rule_shape(rule: RuleSpec) -> str:
    """Compare executable interpretations in memory, excluding bookkeeping."""
    value = rule.model_dump(mode='json', by_alias=True)
    for name in ('revision', 'status', 'validation_ref', 'coverage', 'evidence'):
        value.pop(name, None)
    return canonical(value)


class IterationLedger:
    def __init__(self, store, goal: ExtractionGoal, scopes: list[str]):
        self.store, self.connection = store, store.connection
        self.connection.executescript('''
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
            observation_counts=json.loads(row['observation_counts']))

    def enqueue(self, source: SourceRef, *, task='review_pattern', codes=(),
                rules=(), context_version: str, force=False, observation_counts=None) -> WorkItem:
        codes = sorted(set(codes))
        if set(codes) - REVIEWABLE_CODES:
            raise LightGraphError('unregistered_iteration_pattern')
        counts=dict(observation_counts or {})
        if set(counts)-{'distinct_tuples','conflicts'} or any(type(v) is not int or v<0 for v in counts.values()):
            raise LightGraphError('invalid_iteration_aggregates')
        revisions = {r.rule_id: r.revision for r in rules}
        for ref in [context_version, *revisions]:
            self.store.policy._reference(ref)
        source_ref = canonical(source.model_dump(mode='json', by_alias=True))
        with self.store._transaction():
            rows = self.connection.execute('''SELECT * FROM iteration_work
                WHERE goal_revision=? AND source_ref=? AND task=? AND context_version=?
                ORDER BY rowid DESC''', (self.goal_revision, source_ref, task, context_version)).fetchall()
            for row in rows:
                if (json.loads(row['pattern_codes']) == codes and json.loads(row['rule_revisions']) == revisions
                    and json.loads(row['observation_counts'])==counts):
                    item = self._item(row)
                    if force or item.state in ('deferred', 'resolved'):
                        self.connection.execute("UPDATE iteration_work SET state='pending' WHERE work_id=?", (item.work_id,))
                        item.state = 'pending'
                    return item
            item = WorkItem(work_id='work_' + uuid.uuid4().hex, goal_revision=self.goal_revision,
                source=source, task=task, pattern_codes=codes, rule_revisions=revisions, context_version=context_version,
                observation_counts=counts)
            self.connection.execute('INSERT INTO iteration_work VALUES(?,?,?,?,?,?,?,?,?,?)',
                (item.work_id, self.goal_revision, source_ref, task, context_version,
                 canonical(codes), canonical(revisions), item.state, item.attempts,canonical(counts)))
            return item

    def transition(self, item: WorkItem, state: WorkState):
        if state not in ('running', 'resolved', 'unresolved', 'deferred'):
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

    def resolve_table(self, source: SourceRef):
        """Successful full validation resolves prior failures without model calls."""
        with self.store._transaction():
            self.connection.execute('''UPDATE iteration_work SET state='resolved'
                WHERE goal_revision=? AND source_ref=? AND task='review_pattern' ''',
                (self.goal_revision, canonical(source.model_dump(mode='json', by_alias=True))))

    def unresolved_tables(self, source_id: str, contexts: dict[str, str]) -> set[str]:
        return {i.source.schema_name + '.' + i.source.table for i in self.items()
            if i.source.source_id == source_id and i.task == 'review_pattern' and i.state == 'unresolved'
            and contexts.get(i.source.schema_name + '.' + i.source.table) == i.context_version}

    def items(self) -> list[WorkItem]:
        return [self._item(row) for row in self.connection.execute(
            'SELECT * FROM iteration_work WHERE goal_revision=? ORDER BY rowid', (self.goal_revision,))]

    def context(self, items, rules, *, iteration: int) -> dict:
        selected = {(i.source.source_id, i.source.schema_name, i.source.table) for i in items}
        relevant = [r for r in rules if (r.source.source_id, r.source.schema_name, r.source.table) in selected]
        # Mappings only: no observations or query filter values in "knowledge".
        known = [{'rule_id': r.rule_id, 'revision': r.revision, 'kind': r.kind,
                  'source': r.source.model_dump(mode='json', by_alias=True),
                  'entity_kind': r.identity.entity_kind, 'key_columns': r.identity.key_columns,
                  'attributes': r.attributes.model_dump(mode='json'),
                  'level_policy': r.identity.level_policy} for r in relevant[:24]]
        return {'goal_revision': self.goal_revision, 'iteration': iteration,
                'work_items': [i.model_dump(mode='json', by_alias=True) for i in items[:32]],
                'known_rules': known, 'known_rules_total': len(relevant),
                'knowledge_truncated': len(relevant) > len(known)}

    def counts(self) -> dict[str, int]:
        result = {f'work_{s}': 0 for s in ('pending', 'running', 'resolved', 'unresolved', 'deferred')}
        for item in self.items():
            result['work_' + item.state] += 1
        return result
