"""Public edit forms refer to an exact encrypted connection held by this project."""
from __future__ import annotations

import hmac
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..errors import LightGraphError
from .database_access import ConnectionSpec
from .connection_drafts import DraftConnectionForm


class ConnectionReference(BaseModel):
    model_config = ConfigDict(extra='forbid')
    store: Literal['draft', 'profile']
    key: str = Field(min_length=1, max_length=128)
    stamp: str = Field(pattern=r'^[a-f0-9]{64}$')


def public_form(store, pid, kind, key, form):
    public = {**form}
    for field in ('dsn', 'password'):
        if field in public:
            public[field] = ''
    binding = store.edit_stamp(pid, kind, key, form)
    return public, {'store': kind, 'key': key, 'stamp': binding}


def resolve_form(store, workspace, pid, ref, *, draft_key=None):
    # A persisted source profile and its own in-progress edit share one dialog.
    # The unsaved-new-source dialog has a separate credential slot.
    slot = ref.key if ref.store == 'draft' else 'source-' + ref.key
    if draft_key is not None and slot != draft_key:
        raise LightGraphError('saved_connection_changed')
    if ref.store == 'draft':
        saved = store.load_draft(pid, ref.key)
        if saved and ref.key != 'new':
            form_source = saved['draft']['form']
            if not any(ref.key == 'source-' + s.id and s.id == form_source['id'] and s.kind == form_source['kind']
                       for s in workspace.resolved_config(pid).sources):
                raise LightGraphError('saved_connection_changed')
        form = saved['draft']['connection'] if saved else None
    else:
        source = next((s for s in workspace.resolved_config(pid).sources if s.id == ref.key), None)
        saved = store.load(pid, source) if source else None
        form = saved['connection'] if saved else None
    if form is None or not hmac.compare_digest(ref.stamp, store.edit_stamp(pid, ref.store, ref.key, form)):
        raise LightGraphError('saved_connection_changed')
    return form


def connection_from_form(form):
    if 'format' not in form:
        return ConnectionSpec.model_validate(form)
    dsn = form['format'] == 'dsn'
    fields = ['dsn', 'schema'] if dsn else ['host', 'port', 'database', 'username', 'password', 'schema']
    if dsn and form['dsn'].strip().startswith('jdbc:'):
        fields.extend(['username', 'password'])
    # Apply the same normalization as a newly submitted edit. An unfinished
    # draft may contain padding or a blank port. Password whitespace is data.
    values = {key: form[key] if key == 'password' else form[key].strip() for key in fields if form.get(key)}
    if not dsn and not values.get('port'):
        raise LightGraphError('database_connection_fields_required')
    if form['kind'] == 'gauss_database' and form.get('sslmode') not in (None, 'auto'):
        values['sslmode'] = form['sslmode']
    try:
        return ConnectionSpec.model_validate({'kind': form['kind'], **values})
    except ValidationError:
        raise LightGraphError('database_connection_fields_required') from None


def restore_draft_secrets(draft, saved_form):
    # The reference selects the entire original destination. Never combine a
    # stored password with a host, user, DSN or TLS policy supplied by the caller.
    if 'format' in saved_form:
        return draft.model_copy(update={'connection': DraftConnectionForm.model_validate(saved_form)})
    kind = saved_form['kind']
    form = {'kind': kind, 'format': 'dsn' if saved_form.get('dsn') else 'fields',
            'port': str(saved_form.get('port') or (3306 if kind == 'mysql_database' else 5432)),
            'sslmode': saved_form.get('sslmode') or 'auto'}
    for field in ('dsn', 'host', 'database', 'username', 'password', 'schema'):
        form[field] = saved_form.get(field) or ''
    return draft.model_copy(update={'connection': DraftConnectionForm.model_validate(form)})
