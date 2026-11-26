"""Opaque bindings for environment-supplied database connections."""
import os

from lightgraph.contracts import digest
from lightgraph.errors import LightGraphError


def validate_environment_credentials(sources, source_credentials=None):
    """An environment source cannot borrow a different session connection."""
    for source in sources:
        if source.kind in {'gauss_database', 'mysql_database'} and source.credential_mode == 'environment':
            supplied = (source_credentials or {}).get(source.id)
            if supplied and supplied != os.getenv(source.dsn_env or ''):
                raise LightGraphError('source_environment_override_requires_session')


def validate_environment_adapters(sources, adapters):
    """Check source bindings supplied by concrete database adapters and scoped views."""
    for source in sources:
        if source.kind not in {'gauss_database', 'mysql_database'} or source.credential_mode != 'environment':
            continue
        adapter = adapters.get(source.id)
        getter = getattr(adapter, 'observation_binding', None)
        binding = getter() if getter else None
        if binding and binding != digest(source.kind.removesuffix('_database'), os.getenv(source.dsn_env or '')):
            raise LightGraphError('source_environment_override_requires_session')


def environment_source_bindings(sources, source_credentials=None):
    """Bind the effective connection without persisting a DSN or credential.

    Absence is also a binding: removing a credential must not authorize reuse of
    a checkpoint produced by a previously configured database.
    """
    validate_environment_credentials(sources, source_credentials)
    return {source.id: digest('environment_source_binding_v1', source.kind,
                             (source_credentials or {}).get(source.id) or os.getenv(source.dsn_env or ''))
            for source in sources if source.kind in {'gauss_database', 'mysql_database'}
            and source.credential_mode == 'environment'}
