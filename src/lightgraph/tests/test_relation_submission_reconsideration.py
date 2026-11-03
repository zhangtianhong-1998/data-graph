"""A completed endpoint-context reconsideration supersedes only its own session."""
from collections import Counter
from types import SimpleNamespace

import pytest

from lightgraph.contracts import RuleSpec
from lightgraph.discovery.budget import BudgetAllocator
from lightgraph.discovery.engine import _column_id
from lightgraph.discovery.models import ProposalBatch
from lightgraph.discovery.workflow import discover_references_with_agent
from lightgraph.errors import LightGraphError
from lightgraph.pipeline import _discovery_outcome
from lightgraph.sources import MemorySource
from lightgraph.tests.test_agent_pipeline import table
from lightgraph.tests.test_call_context import document


def inputs():
    tables = [table(name, ['code', 'target']) for name in ('a', 'b', 'unrelated')]
    rules = [RuleSpec(rule_id=name, kind='column_group_entity',
        source={'source_id': 's', 'table': name},
        identity={'key_columns': ['code'], 'entity_kind': 'ReportItem'},
        status='validated', validation_ref='validated_' + name) for name in ('a', 'b')]
    documents = [document(name, 'Each code identifies a report item.',
                         binding='s:public.' + name) for name in ('a', 'b', 'unrelated')]
    rows = {t.qualified_name: [('A', 'B')] for t in tables}
    return tables, rules, documents, rows


class ReconsideringModel:
    config = SimpleNamespace(max_discovery_steps=8, max_tool_calls=8, allow_source_samples=False)

    def __init__(self, tables, final):
        self.tables = {t.name: t for t in tables}
        self.final = final
        self.budget = BudgetAllocator()
        self.calls = Counter()

    def explore(self, packet, tools, **kwargs):
        name = packet['scope']['table']
        self.calls[name] += 1
        context = packet.get('context_tables')
        tools.begin_context([self.tables[ref['table']] for ref in context]
                            if context else [self.tables[name]])
        if name == 'unrelated':
            batch = ProposalBatch(task=packet['task'], abstain=True)
            batch._runtime_pending_codes = ['discovery_submission_incomplete', 'model_response_truncated']
            return batch
        assert name == 'a'
        if self.calls[name] == 1:
            assert {d['doc_id'] for d in packet['document_snippets']} == {'a'}
            batch = ProposalBatch.model_validate({'task': packet['task'], 'proposals': [{
                'kind': 'reference', 'claim': {'source_entity': 'a', 'target_entity': 'b',
                    'source_columns': [_column_id(self.tables['a'], 'code')],
                    'target_columns': [_column_id(self.tables['a'], 'target')],
                    'relation_kind': 'parent_of'},
                'evidence_cited': [_column_id(self.tables['a'], 'code')]}]})
            batch._runtime_pending_codes = ['discovery_submission_pending']
            batch._runtime_invalid_drafts = 1
            batch._runtime_invalid_codes = {'invalid_tool_arguments': 1}
            batch._runtime_invalid_diagnostics = [{'code': 'invalid_tool_arguments',
                'path': ['claim', 'source_columns', 0], 'constraint': 'enum', 'count': 1}]
            return batch
        assert self.calls[name] == 2
        assert {d['doc_id'] for d in packet['document_snippets']} == {'a', 'b'}
        if self.final == 'protocol_failure':
            raise LightGraphError('llm_schema_invalid')
        batch = ProposalBatch(task=packet['task'], abstain=True)
        batch._runtime_submission_complete = self.final in ('finished', 'abstained')
        batch._runtime_completed_empty = self.final == 'finished'
        if self.final == 'pending':
            batch._runtime_pending_codes = ['discovery_submission_pending']
        elif self.final == 'truncated':
            batch._runtime_pending_codes = ['discovery_submission_incomplete', 'model_response_truncated']
        elif self.final == 'mapping_conflict':
            batch._runtime_pending_codes = ['object_model_mapping_conflict']
        return batch


@pytest.mark.parametrize('final,state,outcome', [
    ('finished', 'resolved', 'COMPLETED'),
    ('abstained', 'unresolved', 'COMPLETED_WITH_GAPS'),
    ('pending', 'deferred', 'PARTIAL'),
    ('truncated', 'deferred', 'PARTIAL'),
    ('protocol_failure', 'deferred', 'PARTIAL'),
    ('mapping_conflict', 'deferred', 'PARTIAL'),
    # An empty/error batch's abstain flag alone is not proof of an explicit end.
    ('unattested', 'deferred', 'PARTIAL'),
])
def test_endpoint_context_reconsideration_uses_current_submission_state(final, state, outcome):
    tables, rules, documents, rows = inputs()
    model = ReconsideringModel(tables, final)
    with MemorySource(tables, rows) as source:
        result = discover_references_with_agent(source, rules, documents=documents,
            model=model, table_refs=['public.a'])
    assert model.calls == {'a': 2}
    assert result.rules == []  # The earlier unreviewed proposal is never published.
    assert result.relation_table_outcomes == {'public.a': state}
    assert _discovery_outcome(result.issues) == outcome
    assert result.stats['discovery_invalid_drafts'] == 1
    # Keep historical failures and their field diagnostics for inspection.
    assert result.stats['discovery_pending_submissions'] >= 1
    assert any(i['code'] == 'discovery_draft_field_diagnostic' and i['status'] == 'diagnostic'
               for i in result.issues)
    old = next(i for i in result.issues if i['code'] == 'discovery_submission_pending')
    assert old['status'] == ('diagnostic' if final in ('finished', 'abstained') else 'deferred')


def test_completed_reconsideration_does_not_clear_another_tables_interruption():
    tables, rules, documents, rows = inputs()
    # Process the interrupted table first, then complete A's reconsideration.
    tables = [tables[2], *tables[:2]]
    model = ReconsideringModel(tables, 'finished')
    with MemorySource(tables, rows) as source:
        result = discover_references_with_agent(source, rules, documents=documents,
            model=model, table_refs=['public.a', 'public.unrelated'])
    assert model.calls == {'unrelated': 1, 'a': 2}
    assert result.relation_table_outcomes == {'public.unrelated': 'deferred', 'public.a': 'resolved'}
    assert _discovery_outcome(result.issues) == 'PARTIAL'
    interrupted = [i for i in result.issues if i['table'] == 'public.unrelated'
                   and i['code'] in ('discovery_submission_incomplete', 'model_response_truncated')]
    assert len(interrupted) == 2 and all(i['status'] == 'deferred' for i in interrupted)


def test_runtime_finish_after_document_reconsideration_retires_earlier_pending(monkeypatch):
    from lightgraph.tests.test_agent_pipeline import client, input_packet, response
    from lightgraph.tests.test_proposal_session import tools_response
    tables, rules, documents, rows = inputs()
    calls = []

    def handler(request):
        _, packet = input_packet(request)
        docs = {d['doc_id'] for d in packet['document_snippets']}
        calls.append(docs)
        if docs == {'a'}:
            bindings = {b['source']['table']: b['rule_id'] for b in packet['entity_bindings']}
            columns = {c['name']: c['id'] for c in packet['metadata_digest']['columns']}
            return tools_response(('submit_reference', {'claim': {
                'source_entity': bindings['a'], 'target_entity': bindings['b'],
                'source_columns': [columns['code']], 'target_columns': [columns['target']],
                'relation_kind': 'parent_of'}, 'evidence_cited': [columns['code']]}),
                ('finish_discovery', {'pending': True}))
        assert docs == {'a', 'b'}
        return response({'action': 'finish_discovery', 'pending': False})

    with MemorySource(tables, rows) as source:
        result = discover_references_with_agent(source, rules, documents=documents,
            model=client(monkeypatch, handler), table_refs=['public.a'])
    assert calls == [{'a'}, {'a', 'b'}]
    assert result.rules == [] and result.relation_table_outcomes == {'public.a': 'resolved'}
    assert _discovery_outcome(result.issues) == 'COMPLETED'
    assert any(i['code'] == 'discovery_submission_pending' and i['status'] == 'diagnostic'
               for i in result.issues)
