"""Read-only inventory of the old source-value-derived partition token format.

This is an inventory, not a proof of absence of every possible encoding. It
reports counts only; it never prints the old tokens, source keys or payloads.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sqlite3


_LEGACY = re.compile(r"partition_[0-9a-f]{64}\Z")


def audit_legacy_partition_tokens(connection: sqlite3.Connection) -> dict:
    """Count matching cells without changing historical publications or supports."""
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    counts = {}
    for table in ("publication", "fact_support", "partition_head", "window_partition"):
        if table in tables:
            counts[table] = sum(bool(_LEGACY.fullmatch(row[0])) for row in
                                connection.execute(f"SELECT partition_id FROM {table}"))
    for table in ("validation_record", "rule"):
        if table in tables:
            total = 0
            for row in connection.execute(f"SELECT payload FROM {table}"):
                payload = json.loads(row[0])
                total += sum(bool(_LEGACY.fullmatch(token)) for token in
                             payload.get("coverage", {}).get("partitions", []) if isinstance(token, str))
            counts[table] = total
    return {"token_format": "legacy_partition_sha256", "counts": counts,
            "matching_cells": sum(counts.values()), "migration_required": any(counts.values()),
            "modified": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--fail-on-findings", action="store_true")
    args = parser.parse_args()
    uri = args.database.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        report = audit_legacy_partition_tokens(connection)
    print(json.dumps(report, sort_keys=True))
    return int(args.fail_on_findings and report["migration_required"])


if __name__ == "__main__":
    raise SystemExit(main())
