"""Small Gauss driver differences shared by onboarding and extraction."""
from __future__ import annotations

import logging
import re

from lightgraph.errors import LightGraphError


def catalog_value(value):
    """Decode catalog text only; never repair a corrupted identifier or row."""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            raise LightGraphError("database_catalog_encoding_invalid") from None
    return value


def catalog_bool(value) -> bool:
    value = catalog_value(value)
    if value is True or value == 1 or value in ("t", "true", "on", "1"):
        return True
    if value is False or value == 0 or value in ("f", "false", "off", "0"):
        return False
    raise LightGraphError("database_catalog_boolean_invalid")


def sqlstate(exc: Exception) -> str | None:
    value = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
    return value if isinstance(value, str) and re.fullmatch(r"[0-9A-Z]{5}", value) else None


class SafeDatabaseCause(Exception):
    """Diagnostic classification without driver text, SQL or endpoint details."""

    def __init__(self, state: str | None):
        self.sqlstate = state
        self.category = ({"08": "connection", "28": "authentication", "42": "configuration_or_catalog",
                          "53": "resource", "57": "cancel_or_shutdown", "0A": "unsupported"}
                         .get(state[:2] if state else "", "driver"))
        super().__init__(f"database_driver_error category={self.category} sqlstate={state or 'unknown'}")


def safe_database_cause(exc: Exception, logger: logging.Logger) -> SafeDatabaseCause:
    cause = SafeDatabaseCause(sqlstate(exc))
    logger.error("%s", cause)
    return cause


def set_optional_lock_timeout(cursor, milliseconds: int) -> bool:
    """Probe an optional setting without aborting or replacing the transaction.

    Only an unsupported setting is recoverable. A savepoint leaves the current
    read-only snapshot and statement timeout intact, including on openGauss.
    """
    cursor.execute("SAVEPOINT lightgraph_lock_timeout")
    try:
        cursor.execute(f"SET LOCAL lock_timeout = '{int(milliseconds)}ms'")
    except Exception as exc:
        if sqlstate(exc) not in {"42704", "0A000"}:
            raise
        cursor.execute("ROLLBACK TO SAVEPOINT lightgraph_lock_timeout")
        cursor.execute("RELEASE SAVEPOINT lightgraph_lock_timeout")
        return False
    cursor.execute("RELEASE SAVEPOINT lightgraph_lock_timeout")
    return True
