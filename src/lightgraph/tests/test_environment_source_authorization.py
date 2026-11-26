"""Actual source connections cannot be published under a different environment binding."""
import pytest

from lightgraph.construction.tools import ConstructionTools
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import RunConfig, open_sources
from lightgraph.sources.scoped import GoalScopedSource
from lightgraph.tests.test_relation_projection_evidence import source


@pytest.mark.parametrize('kind', ['mysql', 'gauss'])
def test_common_factory_rejects_mismatched_environment_before_opening_any_source(monkeypatch, kind):
    import lightgraph.sources as adapters
    opened = []
    monkeypatch.setattr(adapters, 'MySQLSource', lambda dsn, **kw: opened.append(dsn) or object())
    monkeypatch.setattr(adapters, 'GaussSource', lambda dsn, **kw: opened.append(dsn) or object())
    monkeypatch.setenv('AUTHORIZATION_DSN', 'configured-connection')
    config = RunConfig(sources=[dict(id='db', kind=kind + '_database',
        dsn_env='AUTHORIZATION_DSN', allowed_tables=['public.items'])])
    for mode in ('environment', 'session'):
        config.sources[0].credential_mode = mode
        if mode == 'environment':
            with pytest.raises(LightGraphError, match='source_environment_override_requires_session'):
                open_sources(config, metadata_only=True, source_credentials={'db': 'different-secret-connection'})
            assert not opened
            open_sources(config, metadata_only=True, source_credentials={'db': 'configured-connection'})
            assert opened == ['configured-connection']
            monkeypatch.delenv('AUTHORIZATION_DSN')
            with pytest.raises(LightGraphError, match='source_environment_override_requires_session'):
                open_sources(config, metadata_only=True, source_credentials={'db': 'configured-connection'})
        else:
            open_sources(config, metadata_only=True, source_credentials={'db': 'session-connection'})
            assert opened[-1] == 'session-connection'


@pytest.mark.parametrize('kind', ['mysql', 'gauss'])
@pytest.mark.parametrize('scoped', [False, True])
def test_construction_checks_borrowed_database_binding_and_runtime_environment(tmp_path, monkeypatch, kind, scoped):
    with source(tmp_path, kind) as (adapter, source_id, schema):
        dsn = 'mysql://tester:secret@localhost/analytics' if kind == 'mysql' else 'test-dsn'
        config = RunConfig(sources=[dict(id=source_id, kind=kind + '_database',
            dsn_env='AUTHORIZATION_DSN', allowed_tables=[schema + '.items'])])
        borrowed = GoalScopedSource(adapter, config.goal) if scoped else adapter
        monkeypatch.setenv('AUTHORIZATION_DSN', 'different-connection')
        directory = tmp_path / 'denied'
        with pytest.raises(LightGraphError, match='source_environment_override_requires_session'):
            ConstructionTools(config, directory, adapters=[borrowed])
        assert not (directory / 'manifest.yaml').exists()
        monkeypatch.setenv('AUTHORIZATION_DSN', dsn)
        tools = ConstructionTools(config, tmp_path / 'accepted', adapters=[borrowed])
        try:
            tools._check_current_inputs()
            monkeypatch.setenv('AUTHORIZATION_DSN', 'changed-connection')
            with pytest.raises(LightGraphError, match='source_environment_override_requires_session'):
                tools._check_current_inputs()
        finally:
            tools.close()
        config.sources[0].credential_mode = 'session'
        tools = ConstructionTools(config, tmp_path / 'session', adapters=[borrowed])
        try:
            tools._check_current_inputs()
        finally:
            tools.close()
