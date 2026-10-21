"""Both databases enter the same guarded runtime and workspace contracts."""
from pathlib import Path

import pytest
import yaml

from lightgraph.contracts import CapabilityManifest, ColumnSpec, ExtractionGoal, TableSpec
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import RunConfig, open_sources
from lightgraph.web.workspace import Workspace


def config(**values):
    return RunConfig(sources=[dict(id='mysql', kind='mysql_database', dsn_env='TEST_MYSQL_DSN',
                                  allowed_tables=['finance.metrics'])], **values)


@pytest.mark.parametrize('metadata_only', [True, False])
def test_gauss_and_mysql_share_budget_and_keep_adapter_selection(monkeypatch, metadata_only):
    import lightgraph.sources as adapters
    calls = []
    class Source:
        def __init__(self, kind, dsn, **kwargs):
            calls.append((kind, dsn, kwargs))
        def close(self):
            pass
    monkeypatch.setattr(adapters, 'GaussSource', lambda dsn, **kw: Source('gauss', dsn, **kw))
    monkeypatch.setattr(adapters, 'MySQLSource', lambda dsn, **kw: Source('mysql', dsn, **kw))
    monkeypatch.setenv('TEST_MYSQL_DSN', 'mysql://placeholder')
    monkeypatch.setenv('TEST_GAUSS_DSN', 'gauss-placeholder')
    current = config(deployment_retention_verified=not metadata_only)
    from lightgraph.runtime.config import SourceConfig
    current.sources.append(SourceConfig(id='gauss', kind='gauss_database', dsn_env='TEST_GAUSS_DSN',
                                       allowed_tables=['finance.metrics']))
    result = open_sources(current, metadata_only=metadata_only)
    assert len(result) == 2
    assert [c[0] for c in calls] == ['mysql', 'gauss']
    assert calls[0][2]['query_budget'] is calls[1][2]['query_budget']
    assert calls[0][2]['allowed_tables'] == ['finance.metrics']


def test_mysql_requires_deployment_confirmation_and_credential_env(monkeypatch):
    monkeypatch.delenv('TEST_MYSQL_DSN', raising=False)
    with pytest.raises(LightGraphError, match='deployment_retention_unverified'):
        open_sources(config())
    with pytest.raises(LightGraphError, match='source_credential_missing'):
        open_sources(config(), metadata_only=True)


def test_both_database_manifests_accept_yaml_and_bind_source(tmp_path, monkeypatch):
    import lightgraph.sources as adapters
    captured = []
    monkeypatch.setattr(adapters, 'MySQLSource', lambda *a, **kw: captured.append(kw) or object())
    monkeypatch.setenv('TEST_MYSQL_DSN', 'mysql://placeholder')
    manifest = CapabilityManifest(product='MySQL', version='8.4.0', driver='PyMySQL/test',
                                  capabilities={'query_cancel': 'verified'})
    file = tmp_path / 'capabilities.yaml'
    file.write_text(yaml.safe_dump({'mysql': manifest.model_dump(mode='json')}))
    current = config(deployment_retention_verified=True)
    current.sources[0].capability_manifest = str(file)
    open_sources(current)
    assert captured[0]['verified_capabilities'] == manifest
    file.write_text('mysql: &ref {}\nother: *ref')
    with pytest.raises(LightGraphError, match='capability_manifest_invalid'):
        open_sources(current)


def test_opening_second_database_failure_closes_first(monkeypatch):
    import lightgraph.sources as adapters
    closed = []
    class Source:
        def close(self):
            closed.append(True)
    monkeypatch.setattr(adapters, 'MySQLSource', lambda *a, **kw: Source())
    monkeypatch.setenv('TEST_MYSQL_DSN', 'mysql://placeholder')
    current = config()
    from lightgraph.runtime.config import SourceConfig
    current.sources.append(SourceConfig(id='missing', kind='gauss_database', dsn_env='ABSENT_DB_CREDENTIAL',
                                       allowed_tables=['finance.metrics']))
    monkeypatch.delenv('ABSENT_DB_CREDENTIAL', raising=False)
    with pytest.raises(LightGraphError, match='source_credential_missing'):
        open_sources(current, metadata_only=True)
    assert closed == [True]


def test_workspace_persists_mixed_database_config_without_credentials(tmp_path):
    workspace = Workspace(tmp_path / 'workspace')
    workspace.create('mixed', 'mixed')
    value = workspace.get_config('mixed')
    value['sources'] = [dict(id='mysql', kind='mysql_database', dsn_env='MYSQL_DSN', allowed_tables=['finance.items']),
                        dict(id='gauss', kind='gauss_database', dsn_env='GAUSS_DSN', allowed_tables=['finance.items'])]
    saved = workspace.save_config('mixed', value)
    assert [s['kind'] for s in saved['sources']] == ['mysql_database', 'gauss_database']
    assert saved['isolation_domain'] == 'mixed'
    file = workspace.add_file('mixed', 'fake.csv', b'a\nx\n')
    value['sources'][0]['path'] = file['relative_path']
    with pytest.raises(LightGraphError, match='database_source_cannot_have_file_path'):
        workspace.save_config('mixed', value)


def test_agent_receives_mysql_dialect_without_connection_information():
    from lightgraph.sources import MemorySource
    from lightgraph.query import QueryCompiler
    from lightgraph.discovery.tools import ToolExecutor
    from lightgraph.discovery.budget import BudgetAllocator
    table = TableSpec(source_id='mysql', schema='finance', name='metrics', columns=[ColumnSpec(name='code')])
    adapter = MemorySource([table], {'finance.metrics': []})
    adapter.compiler = QueryCompiler([table], dialect='mysql')
    executor = ToolExecutor(adapter, ExtractionGoal(), BudgetAllocator())
    try:
        response = executor.execute('get_metadata', {})
        assert response['data']['tables'][0]['sql_dialect'] == 'mysql'
        sql_tool = next(t['function'] for t in executor.tool_specs() if t['function']['name'] == 'execute_readonly_sql')
        assert 'mysql dialect' in sql_tool['description']
    finally:
        executor.close()
        adapter.close()
