"""Column definitions remain independently verifiable after a fact source stops."""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lightgraph.construction.definitions import definition_options, metadata_definition_draft
from lightgraph.construction.models import DraftRecord, GraphDraft
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import dump_yaml
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import RunConfig
from lightgraph.tests.test_construction_metadata_columns import (
    TrackedSource, column_draft, physical_table, row_draft,
)


def parent_record(**updates):
    raw = row_draft()
    raw.update(purpose='Discover indicators, dimensions and measures',
        rationale='Column meaning is supported by the saved catalog.', evidence_refs=['e-global'],
        unresolved=['The indicator-to-measure relationship still needs complete verification.'],
        decisions=['Physical column identity is not a fact value.'], suggestions=['Optional dictionary review.'])
    return DraftRecord(draft=GraphDraft.model_validate(raw), revision=2,
        fingerprint='parent-fingerprint', context_fingerprint='current-context', **updates)


def test_derivation_preserves_metadata_and_parent_without_claiming_fact_completion():
    parent = parent_record(status='needs_revision')
    original = parent.model_dump(mode='json', by_alias=True)
    draft = metadata_definition_draft(parent)
    assert draft == metadata_definition_draft(parent)
    assert draft.draft_id != parent.draft.draft_id and len(draft.draft_id) <= 80
    assert draft.entities == [parent.draft.entities[1]] and not draft.relations
    assert draft.entities[0] is not parent.draft.entities[1]
    assert draft.evidence_refs == parent.draft.evidence_refs
    assert draft.rationale == parent.draft.rationale and draft.suggestions == parent.draft.suggestions
    assert draft.decisions[:1] == parent.draft.decisions
    assert draft.unresolved[:1] == parent.draft.unresolved
    assert any('parent-fingerprint' in item for item in draft.decisions)
    assert 'revision 2' in draft.decisions[-1] and 'does not resolve' in draft.decisions[-1]
    assert draft.unresolved == parent.draft.unresolved
    assert parent.model_dump(mode='json', by_alias=True) == original
    assert metadata_definition_draft(parent.model_copy(update={'revision': 3})).draft_id != draft.draft_id
    assert definition_options(parent)['arguments'] == {'metadata_from_draft': 'analysis'}


def test_derivation_retains_maximum_length_notes_and_long_parent_id():
    parent = parent_record()
    parent.draft.draft_id = 'd' * 80
    parent.draft.decisions = ['decision-' + str(n) for n in range(128)]
    parent.draft.unresolved = ['gap-' + str(n) for n in range(128)]
    child = metadata_definition_draft(parent)
    assert len(child.draft_id) <= 80
    assert len(child.decisions) == len(child.unresolved) == 128
    assert child.decisions[-1].startswith('decision-127\n')
    assert child.unresolved == parent.draft.unresolved
    assert parent.draft.decisions[-1] == 'decision-127'


def test_no_stale_empty_or_recursive_derivation():
    stale = parent_record(status='stale')
    with pytest.raises(LightGraphError, match='stale_graph_draft'):
        metadata_definition_draft(stale)
    assert not definition_options(stale)['available']
    empty = parent_record()
    empty.draft.relations = []
    empty.draft.entities = empty.draft.entities[:1]
    with pytest.raises(LightGraphError, match='metadata_definitions_unavailable'):
        metadata_definition_draft(empty)
    child = parent_record().model_copy(update={'draft': GraphDraft.model_validate(column_draft())})
    with pytest.raises(LightGraphError, match='metadata_definition_draft_not_needed'):
        metadata_definition_draft(child)
    assert definition_options(child) == {'available': False, 'reason': 'already_metadata_only'}


class LostFactConnection(TrackedSource):
    terminal_failure_code = None
    terminal_failure_diagnostic = None

    @contextmanager
    def snapshot(self):
        self.snapshots += 1
        self.terminal_failure_code = 'database_reconnect_failed'
        self.terminal_failure_diagnostic = {'engine': 'gauss', 'category': 'connection',
            'stage': 'reconnect', 'terminal': True, 'sqlstate': '08006'}
        error = LightGraphError(self.terminal_failure_code)
        error.database_diagnostic = self.terminal_failure_diagnostic
        raise error
        yield  # Make the synthetic failure follow the production context manager protocol.


@pytest.fixture
def failed_fact_draft(tmp_path):
    table = physical_table()
    source = LostFactConnection([table], {table.qualified_name: [('I1', '收入', 'O1', '办事处', 'A', 1)]})
    tools = ConstructionTools(RunConfig(sources=[], model={'mode': 'in_environment'}), tmp_path / 'work', adapters=[source])
    try:
        evidence = tools.call('inspect_source', table='facts:public.observations', operation='get_metadata')
        authored = row_draft(ref=evidence['evidence_ref'])
        assert tools.call('save_graph_draft', yaml_text=dump_yaml(authored))['status'] == 'ok'
        result = tools.call('validate_graph_draft', draft_id='analysis')
        assert not result['passed']
        assert source.snapshots == 1 and source.terminal_failure_code
        yield tools, source, authored, result
    finally:
        tools.close()
        source.close()


def test_catalog_definitions_validate_and_publish_after_fact_circuit_and_closeout(failed_fact_draft):
    tools, source, authored, failed = failed_fact_draft
    assert failed['statistics']['nodes'] == 1  # Catalog column was already materialized, not queried.
    parent = tools.workspace.get_draft('analysis').model_dump(mode='json', by_alias=True)
    assert failed['metadata_definitions']['available']
    # Stop fact exploration too: this must not prohibit a zero-query validation.
    for _ in range(3):
        tools.call('validate_graph_draft', draft_id='analysis')
    assert tools.terminal_reason == 'source_retry_limit_exhausted'
    before = source.stats.queries, source.snapshots
    derived = tools.call('save_graph_draft', metadata_from_draft='analysis')
    assert derived['status'] == 'ok', derived
    child_id = derived['draft_id']
    assert derived['scope'] == 'metadata_definitions_only'
    check = tools.call('validate_graph_draft', draft_id=child_id)
    assert check['passed'] and check['metrics']['projected_distinct_rows'] == 0, check
    published = tools.call('publish_graph_draft', draft_id=child_id)
    assert published['status'] == 'ok' and published['nodes'] == 1 and published['edges'] == 0
    graph = tools.workspace.read_published_graph()
    assert graph['nodes'][0]['type'] == 'MeasureDefinition'
    assert graph['nodes'][0]['identity']['column'] == 'comparison_total'
    assert (source.stats.queries, source.snapshots) == before
    assert tools.workspace.get_draft('analysis').model_dump(mode='json', by_alias=True) == parent
    # The parent failure is retained; omitted scope is not a new child goal.
    assert tools.workspace.get_draft('analysis').validation.passed is False
    assert tools.call('save_graph_draft', metadata_from_draft=child_id)['code'] == 'metadata_definition_draft_not_needed'


@pytest.mark.parametrize('code,category', [('source_catalog_changed', 'configuration_or_catalog'),
    ('database_reconnect_failed', 'authentication'), ('database_reconnect_failed', 'permission')])
def test_catalog_requalification_cannot_be_bypassed_by_derivation(failed_fact_draft, code, category):
    tools, source, _, _ = failed_fact_draft
    child = tools.call('save_graph_draft', metadata_from_draft='analysis')['draft_id']
    assert tools.call('validate_graph_draft', draft_id=child)['passed']
    # A changed authorization/catalog is stronger evidence than network loss.
    source.terminal_failure_code = code
    source.terminal_failure_diagnostic = {'category': category, 'stage': 'reconnect', 'terminal': True}
    tools.source_circuits.states.clear()
    before = source.snapshots
    assert tools.call('save_graph_draft', metadata_from_draft='analysis')['code'] == 'metadata_catalog_requalification_required'
    assert tools.call('validate_graph_draft', draft_id=child)['code'] == 'metadata_catalog_requalification_required'
    assert tools.call('publish_graph_draft', draft_id=child)['code'] == 'metadata_catalog_requalification_required'
    assert tools.workspace.read_published_graph()['nodes'] == [] and source.snapshots == before


def test_derivation_does_not_repair_inapplicable_evidence(failed_fact_draft):
    tools, source, _, failed = failed_fact_draft
    record = tools.workspace.get_draft('analysis')
    changed = deepcopy(record.draft)
    changed.entities[1].mappings[0].evidence_refs = [failed['evidence_ref']]
    # Simulate a retained older draft; data-validation evidence is not metadata.
    tools.workspace.save_draft(changed)
    result = tools.call('save_graph_draft', metadata_from_draft='analysis')
    assert result['code'] == 'metadata_evidence_not_applicable'
    assert source.snapshots == 1


def test_save_input_selection_is_unambiguous(failed_fact_draft):
    tools, _, authored, _ = failed_fact_draft
    for arguments in ({}, {'yaml_text': dump_yaml(authored), 'metadata_from_draft': 'analysis'}):
        assert tools.call('save_graph_draft', **arguments)['code'] == 'graph_draft_input_selection_required'


@pytest.mark.skipif(os.environ.get('LIGHTGRAPH_TEST_HARNESS') != '1', reason='requires installed native Harness')
def test_native_agent_derives_validates_and_publishes_catalog_after_disconnect(failed_fact_draft, tmp_path):
    from lightgraph.construction.harness import HarnessOptions, run_goal
    from lightgraph.construction.mcp_server import serve_local_tools
    from lightgraph.tests.test_construction_harness_integration import respond

    tools, source, _, _ = failed_fact_draft
    original = tools.workspace.get_draft('analysis')
    requests, errors = [], []
    saved = {}
    before = source.stats.queries, source.snapshots

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(body)
            try:
                step = len(requests)
                if step == 1:
                    name, arguments = 'save_graph_draft', {'metadata_from_draft': 'analysis'}
                else:
                    result = next(m['content'] for m in reversed(body['messages']) if m['role'] == 'tool')
                    if isinstance(result, list):
                        result = ''.join(b.get('text', '') for b in result)
                    result = json.loads(result)
                    assert result['status'] == 'ok', result
                    if step == 2:
                        saved['draft_id'] = result['draft_id']
                        name, arguments = 'validate_graph_draft', {'draft_id': result['draft_id']}
                    elif step == 3:
                        assert result['passed'], result
                        name, arguments = 'publish_graph_draft', {'draft_id': saved['draft_id']}
                    else:
                        assert result['nodes'] == 1 and result['edges'] == 0
                        respond(self, {'role': 'assistant', 'content': 'Column definition published; original fact work remains incomplete.'}, 'stop')
                        return
                respond(self, {'role': 'assistant', 'tool_calls': [{'index': 0,
                    'id': f'call-{step}', 'type': 'function', 'function': {
                        'name': 'mcp__graph__' + name, 'arguments': json.dumps(arguments)}}]}, 'tool_calls')
            except Exception as exc:
                errors.append(repr(exc))
                respond(self, {'role': 'assistant', 'content': 'Test failed.'}, 'stop')

    model = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    thread = threading.Thread(target=model.serve_forever, daemon=True)
    thread.start()
    try:
        path = tmp_path / 'config.yaml'
        path.write_text('goal: retain definitions after a fact source fails\n')
        with serve_local_tools(tools) as (url, token):
            result = run_goal('Derive existing column definitions from analysis, validate and publish the child; keep the failed parent.',
                path, tmp_path / 'runtime', HarnessOptions(model='local-test',
                    base_url=f'http://127.0.0.1:{model.server_port}/v1', max_calls=6, max_run_seconds=45),
                mcp_url=url, mcp_token_env='GRAPH_TEST_TOKEN',
                environment={'GRAPH_TEST_TOKEN': token, 'OPENAI_API_KEY': 'local-test-only'})
        assert not errors, errors
        assert result['finish_reason'] == 'completed' and len(requests) == 4
        assert tools.workspace.get_draft('analysis') == original
        assert (source.stats.queries, source.snapshots) == before
        assert len(tools.workspace.read_published_graph()['nodes']) == 1
    finally:
        model.shutdown()
        model.server_close()
        thread.join(timeout=5)
