"""An authorized subgraph travels to an isolated SQL-drafting turn without a database."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from lightgraph.errors import LightGraphError
from lightgraph.retrieval.agent_context import agent_context
from lightgraph.runtime.config import RunConfig
from lightgraph.web.sql_preview import _catalogs, _sql_result, draft_sql


def _fixture():
    config = RunConfig(sources=[{'id': 'museum', 'kind': 'gauss_database', 'allowed_tables': ['archive.finds']}])
    observation = {'source_tables': ['museum.archive.finds'], 'provenance': {
        'source': {'source_id': 'museum', 'schema': 'archive', 'table': 'finds'},
        'columns': ['tag', 'weight'], 'key_columns': ['tag']},
        'business_context': {'status': 'scoped', 'dimensions': {'collection': 'A'}},
        'properties': {'unit': ['grams']},
        'row_context': {'values': {'sample': 'RAW_ROW_CANARY'}}}
    answer = {'query': 'Total weight of finds?', 'plan': {'intent': 'search'}, 'nodes': [
        {'id': 'n1', 'kind': 'specimen', 'display_name': 'Find', 'observations': [observation]},
        {'id': 'n2', 'kind': 'measurement', 'display_name': 'Weight'}],
        'edges': [{'id': 'e1', 'relation': 'measured_by', 'source': 'n1', 'target': 'n2'}],
        'traverse': {'paths': [{'node_ids': ['n1', 'n2'], 'edge_ids': ['e1']}]}}
    return config, agent_context(answer, version='v1', sources=config.sources)


def test_context_keeps_source_bindings_and_never_serializes_raw_row():
    config, context = _fixture()
    assert context['graph_version'] == 'v1'
    assert context['paths'][0]['edge_ids'] == ['e1'] and context['joint_witnesses'] == []
    assert 'RAW_ROW_CANARY' not in json.dumps(context)
    assert context['bindings'][0]['properties'] == {'unit': ['grams']}
    assert context['bindings'][0]['business_context']['dimensions'] == {'collection': 'A'}
    with pytest.raises(LightGraphError, match='sql_preview_model_unavailable'):
        draft_sql(config, context)


@pytest.mark.parametrize('sql', ['SELECT guessed FROM archive.finds', 'SELECT tag FROM archive.elsewhere',
    'DELETE FROM archive.finds', 'SELECT tag FROM archive.finds; SELECT 1',
    'WITH x AS (DELETE FROM archive.finds RETURNING tag) SELECT tag FROM x'])
def test_preview_rejects_unbound_or_non_query_output(sql):
    _, context = _fixture()
    with pytest.raises(LightGraphError, match='sql_preview_invalid_output'):
        _sql_result(json.dumps({'source_id': 'museum', 'sql': sql}), _catalogs(context))


def test_preview_accepts_bound_aggregate_and_can_report_missing_evidence():
    _, context = _fixture()
    sql = 'WITH m AS (SELECT weight FROM archive.finds) SELECT SUM(weight) FROM m'
    assert _sql_result(json.dumps({'source_id': 'museum', 'sql': sql}), _catalogs(context))['sql'] == sql
    assert _sql_result('{"missing":"Choose the catalogue"}', {})['status'] == 'insufficient_context'


@pytest.mark.skipif(os.environ.get('LIGHTGRAPH_TEST_HARNESS') != '1', reason='requires bootstrapped stock DSH')
def test_stock_dsh_sql_preview_has_no_tools_and_keeps_question_and_context_together():
    from lightgraph.tests.test_construction_harness_integration import respond
    from lightgraph.runtime.config import ModelConfig
    config, context = _fixture()
    requests = []
    sql = 'SELECT SUM(weight) FROM archive.finds'

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            respond(self, {'role': 'assistant', 'content': json.dumps({'source_id': 'museum', 'sql': sql})}, 'stop')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config.model = ModelConfig(mode='in_environment', model='loopback-model',
            base_url=f'http://127.0.0.1:{server.server_port}/v1', timeout_seconds=40)
        result = draft_sql(config, context, 'loopback-only')
        assert result == {'status': 'generated', 'sql': sql, 'source_id': 'museum', 'dialect': 'postgres'}
        assert len(requests) == 1 and not requests[0].get('tools')
        assert 'RAW_ROW_CANARY' not in str(requests)
        assert context['question'] in str(requests) and 'measured_by' in str(requests)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_long_descriptions_do_not_hide_a_visible_physical_binding():
    config, _ = _fixture()
    source = {'source_id': 'museum', 'schema': 'archive', 'table': 'finds'}
    context = agent_context({'nodes': [{'id': 'specimen', 'kind': 'specimen', 'display_name': 'Find',
        'description': 'detail ' * 4000, 'observations': [{'properties': {'document': 'text ' * 6000},
            'provenance': {'source': source, 'key_columns': ['tag'], 'columns': ['tag']}}]}]}, sources=config.sources)
    assert context['truncated'] and context['nodes'][0]['description_omitted']
    assert context['bindings'][0]['properties_omitted']
    assert _catalogs(context)['museum'][1]['archive']['finds'] == {'tag': 'UNKNOWN'}


def test_preview_and_guard_require_complete_scopes(monkeypatch):
    from lightgraph.query import SQLGuard
    from lightgraph.query import guard as guard_module
    from lightgraph.contracts import ColumnSpec, TableSpec
    original = guard_module.traverse_scope
    monkeypatch.setattr(guard_module, 'traverse_scope', lambda tree: list(original(tree))[1:])
    sql = 'WITH selected AS (SELECT tag FROM archive.finds) SELECT tag FROM selected'
    table = TableSpec(source_id='museum', schema='archive', name='finds', columns=[ColumnSpec(name='tag')])
    with pytest.raises(LightGraphError, match='unsupported_query_scope'):
        SQLGuard([table]).validate(sql)
    _, context = _fixture()
    with pytest.raises(LightGraphError, match='sql_preview_invalid_output'):
        _sql_result(json.dumps({'source_id': 'museum', 'sql': sql}), _catalogs(context))


def test_slow_runtime_shutdown_cannot_hold_preview_past_deadline(monkeypatch):
    from threading import Event
    from lightgraph.runtime.config import ModelConfig
    from lightgraph.web import sql_preview

    config, context = _fixture()
    config.model = ModelConfig(mode='in_environment', model='local-fixture',
        base_url='http://127.0.0.1:8123', timeout_seconds=0.2)
    close_started, release = Event(), Event()

    class WaitingRuntime:
        def run(self, prompt):
            release.wait(5)

        def close(self):
            close_started.set()
            release.wait(5)

    monkeypatch.setattr(sql_preview, 'resolved_options', lambda options: options)
    monkeypatch.setattr(sql_preview, '_base_profile', lambda *args: ())
    monkeypatch.setattr(sql_preview, '_sdk_factory', lambda options: lambda **kwargs: WaitingRuntime())
    try:
        with pytest.raises(LightGraphError, match='sql_preview_timeout'):
            draft_sql(config, context, 'local-fixture')
        assert close_started.wait(1)
    finally:
        release.set()
