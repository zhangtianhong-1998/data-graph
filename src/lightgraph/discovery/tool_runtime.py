"""Validate and execute only the tools issued by the caller's discovery executor."""
from __future__ import annotations

import inspect
import json
from copy import deepcopy
from uuid import uuid4

from jsonschema import Draft202012Validator

from lightgraph.errors import BudgetExceeded, LightGraphError
from lightgraph.runtime.trace import emit_trace
from .validation_feedback import schema_diagnostics


class GuardedToolkit:
    def __init__(self, executor, dispatch=None, *, extra_specs=()):
        self._execute = executor.execute if dispatch is None else dispatch
        self.invalid_argument_handler = None
        self._schemas = {}
        self._validators = {}
        for spec in (*executor.tool_specs(), *extra_specs):
            try:
                function = spec['function']
                name = function['name']
                if not isinstance(name, str) or not name or name in self._schemas:
                    raise ValueError
                schema = deepcopy(spec)
                parameters = schema['function']['parameters']
                Draft202012Validator.check_schema(parameters)
                self._validators[name] = Draft202012Validator(parameters)
                self._schemas[name] = schema
            except Exception:
                raise LightGraphError('invalid_tool_schema') from None

    def get_json_schemas(self):
        return deepcopy(list(self._schemas.values()))

    def _argument_diagnostics(self, name, arguments):
        if not isinstance(arguments, dict) or any(not isinstance(key, str) for key in arguments):
            return [{'path': [], 'constraint': 'type'}]
        try:
            if len(json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode()) > 32768:
                return [{'path': [], 'constraint': 'argument_bytes_limit'}]
            return schema_diagnostics(self._validators[name], arguments)
        except (ValueError, TypeError, RecursionError):
            return [{'path': [], 'constraint': 'invalid_json_value'}]

    async def call(self, name, arguments, *, tool_call_id=None):
        invocation = 'tool_' + uuid4().hex
        emit_trace('tool_call', {'invocation_id': invocation,
            'call': {'type': 'tool_use', 'name': name, 'id': tool_call_id, 'input': arguments}})
        try:
            if not isinstance(name, str) or name not in self._schemas:
                value = {'status': 'error', 'operator': 'unknown', 'code': 'unknown_tool'}
            elif diagnostics := self._argument_diagnostics(name, arguments):
                feedback = (self.invalid_argument_handler(name, arguments, diagnostics)
                            if self.invalid_argument_handler else None)
                value = feedback if feedback is not None else {
                    'status': 'error', 'operator': name, 'code': 'invalid_tool_arguments',
                    'diagnostics': diagnostics}
            else:
                try:
                    value = self._execute(name, arguments)
                    if inspect.isawaitable(value):
                        value = await value
                    if not isinstance(value, dict):
                        raise ValueError
                    json.dumps(value, ensure_ascii=False, allow_nan=False)
                except BudgetExceeded:
                    raise
                except Exception:
                    value = {'status': 'error', 'operator': name, 'code': 'tool_execution_failed'}
            emit_trace('tool_result', {'invocation_id': invocation, 'name': name,
                'tool_call_id': tool_call_id, 'content': [
                    {'type': 'text', 'text': json.dumps(value, ensure_ascii=False, allow_nan=False)}]})
            return value
        except BaseException as exc:
            emit_trace('tool_error', {'invocation_id': invocation, 'name': name,
                'code': exc.code if isinstance(exc, LightGraphError) else 'tool_execution_interrupted',
                'exception_type': type(exc).__name__})
            raise

    def clear(self):
        self._schemas.clear()
        self._validators.clear()
        self._execute = None
        self.invalid_argument_handler = None


def create_toolkit(executor, dispatch=None, *, extra_specs=()):
    return GuardedToolkit(executor, dispatch, extra_specs=extra_specs)
