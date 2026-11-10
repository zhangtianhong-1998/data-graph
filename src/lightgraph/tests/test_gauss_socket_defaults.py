"""libpq defaults apply to real-driver calls without replacing explicit DSN values."""
import pytest

from lightgraph.sources.gauss import GaussSource
from lightgraph.tests.test_sources_gauss import Connection


@pytest.mark.parametrize('dsn, expected', [
    ('host=db.example dbname=business user=reader sslmode=verify-full',
     {'keepalives':'1', 'keepalives_idle':'30', 'keepalives_interval':'10', 'keepalives_count':'3'}),
    ('postgresql://reader@db.example/business?sslmode=require',
     {'keepalives':'1', 'keepalives_idle':'30', 'keepalives_interval':'10', 'keepalives_count':'3'}),
    ('host=db.example dbname=business user=reader keepalives=0 keepalives_idle=90',
     {'keepalives':'0', 'keepalives_idle':'90', 'keepalives_interval':'10', 'keepalives_count':'3'}),
    ('postgresql://reader@db.example/business?keepalives=1&keepalives_idle=100&keepalives_interval=20&keepalives_count=8',
     {'keepalives':'1', 'keepalives_idle':'100', 'keepalives_interval':'20', 'keepalives_count':'8'}),
])
def test_gauss_keepalive_defaults_preserve_explicit_dsn_and_readonly_preflight(monkeypatch, dsn, expected):
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    calls, db = [], Connection()
    def connect(value, **options):
        calls.append(conninfo_to_dict(make_conninfo(value, **options)))
        return db
    monkeypatch.setattr('psycopg.connect', connect)
    with GaussSource(dsn, allowed_tables=['public.items']) as source:
        assert source.describe()[0].name == 'items'
    assert len(calls) == 1
    assert {key:calls[0][key] for key in expected} == expected
    explicit = conninfo_to_dict(dsn)
    assert all(calls[0][key] == value for key, value in explicit.items())
    assert int(calls[0]['connect_timeout']) > 0
    assert any('READ ONLY' in sql for sql, _ in db.executed)
    assert db.closed  # Default keepalive never restores a run-long session.
