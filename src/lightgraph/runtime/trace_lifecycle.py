"""Bind a durable trace for one run; releasing resources never deletes it."""
from contextlib import contextmanager
import os
from pathlib import Path
import uuid

from .trace import RunTrace, bind_trace, current_trace, emit_trace


@contextmanager
def execution_trace(config, *, path=None, secrets=()):
    current = current_trace()
    if current is not None:
        current.add_secrets(secrets)
        yield current
        return
    if not config.record_trace:
        yield None
        return
    values = list(secrets)
    settings = config.resolved_model
    if settings.api_key is not None:
        values.append(settings.api_key.get_secret_value())
    values.append(os.environ.get(settings.api_key_env, ''))
    for source in config.sources:
        if source.dsn_env:
            values.append(os.environ.get(source.dsn_env, ''))
    path = path or Path(config.output_dir) / 'traces' / ('run_'+uuid.uuid4().hex+'.trace.jsonl')
    sink = RunTrace(path, secrets=values)
    with bind_trace(sink):
        emit_trace('run_configuration', {'config':config.model_dump(mode='json'),
            'effective_model':settings.model_dump(mode='json')})
        yield sink
