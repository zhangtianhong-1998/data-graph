"""AgentScope tool functions over the existing guarded discovery executor.

Tool observations remain ephemeral. This module neither logs nor persists tool
inputs, outputs, or exception messages. SQL authorization and query budgets stay
inside ToolExecutor; Agent-level dispatch may add loop and metadata-only checks.
"""
from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from contextvars import ContextVar
from copy import deepcopy
from typing import Any

from agentscope import _config
from agentscope.message import TextBlock, ToolUseBlock
from agentscope.tool import Toolkit, ToolResponse
from jsonschema import Draft202012Validator

from lightgraph.errors import BudgetExceeded, LightGraphError


def _response(value: dict[str, Any]) -> ToolResponse:
    return ToolResponse(content=[TextBlock(
        type="text", text=json.dumps(value, ensure_ascii=False, allow_nan=False),
    )])


def _error(code: str, operator: str = "unknown") -> ToolResponse:
    return _response({"status": "error", "operator": operator, "code": code})


class GuardedToolkit(Toolkit):
    """Keep AgentScope execution while containing its exception echo behavior.

    AgentScope converts ordinary exceptions into tool text. Budget exhaustion
    must instead terminate exploration: the function wrapper records it in a
    call-local carrier and the outer iterator re-raises before yielding output.
    """

    def __init__(self):
        super().__init__()
        self._failure: ContextVar[list[BudgetExceeded] | None] = ContextVar(
            "lightgraph_tool_budget_failure", default=None,
        )
        self._argument_validators: dict[str, Draft202012Validator] = {}

    def _valid_arguments(self, name: str, arguments: Any) -> bool:
        """Validate without formatting JSON Schema errors containing source data."""
        if not isinstance(arguments, dict) or any(not isinstance(key, str) for key in arguments):
            return False
        try:
            if len(json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode()) > 32768:
                return False
            validator = self._argument_validators.get(name)
            return validator is not None and validator.is_valid(arguments)
        except Exception:
            return False

    async def call_tool_function(self, tool_call: ToolUseBlock):
        """Return AgentScope's response iterator without exposing invalid input."""
        if _config.trace_enabled:
            # Fail before the framework's tracing decorator receives any data.
            raise LightGraphError("agent_tracing_forbidden")

        async def responses():
            name = tool_call.get("name") if isinstance(tool_call, dict) else None
            if not isinstance(name, str) or name not in self.tools:
                yield _error("unknown_tool")
                return
            arguments = tool_call.get("input", {})
            if not self._valid_arguments(name, arguments):
                yield _error("invalid_tool_arguments", name)
                return
            failures: list[BudgetExceeded] = []
            token = self._failure.set(failures)
            try:
                result = super(GuardedToolkit, self).call_tool_function(tool_call)
                if inspect.isawaitable(result):
                    result = await result
                async for chunk in result:
                    if failures:
                        raise failures[0]
                    yield chunk
                if failures:
                    raise failures[0]
            finally:
                failures.clear()
                self._failure.reset(token)

        return responses()


def create_toolkit(executor, dispatch: Callable[[str, dict[str, Any]], Any] | None = None) -> GuardedToolkit:
    """Register every executor schema as a real AgentScope ToolResponse function.

    ``dispatch(name, arguments)`` may be synchronous or asynchronous. It should
    delegate authorized calls to ``executor.execute`` after any Agent-level loop
    checks. No fixed list of six names is maintained here, so scoped executors
    and test executors retain exactly their own tools and parameter schemas.
    """
    toolkit = GuardedToolkit()
    execute = dispatch or executor.execute

    def wrapper(name: str):
        async def run(**arguments):
            try:
                if not toolkit._valid_arguments(name, arguments):
                    return _error("invalid_tool_arguments", name)
                value = execute(name, arguments)
                if inspect.isawaitable(value):
                    value = await value
                if not isinstance(value, dict):
                    return _error("tool_execution_failed", name)
                return _response(value)
            except BudgetExceeded as error:
                failures = toolkit._failure.get()
                if failures is None:
                    # Direct calls to the registered callable do not pass
                    # through the framework, so they may propagate normally.
                    raise
                failures.append(error)
                return _error("budget_exhausted", name)
            except Exception:
                return _error("tool_execution_failed", name)

        run.__name__ = name
        run.__doc__ = "Execute this discovery operation through the guarded executor."
        return run

    for spec in executor.tool_specs():
        schema = deepcopy(spec)
        try:
            name = schema["function"]["name"]
            if not isinstance(name, str) or not name or name in toolkit.tools:
                raise ValueError
            parameters = schema["function"]["parameters"]
            Draft202012Validator.check_schema(parameters)
            toolkit._argument_validators[name] = Draft202012Validator(parameters)
            toolkit.register_tool_function(
                wrapper(name), func_name=name, json_schema=deepcopy(schema),
                namesake_strategy="raise", async_execution=False,
            )
            # Registration removes empty `required`; preserve the executor's
            # exact public contract instead of regenerating it from **kwargs.
            toolkit.tools[name].json_schema = schema
        except Exception:
            raise LightGraphError("invalid_tool_schema") from None
    return toolkit
