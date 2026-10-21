"""Built-in type equivalences used only for the requested SQL dialect."""
from __future__ import annotations

_MYSQL_TYPES = {
    "tinyint": "smallint", "mediumint": "integer", "double": "double precision",
    "datetime": "timestamp", "year": "integer", "tinytext": "text",
    "mediumtext": "text", "longtext": "text", "enum": "text", "set": "text",
    "binary": "blob", "varbinary": "blob", "tinyblob": "blob",
    "mediumblob": "blob", "longblob": "blob",
}
# Native JSON deliberately stays JSON: the guard admits its projection and the
# registered profiler casts it to CHAR, while Predicate/1.0 must not silently
# grant text equality, IN, or prefix operations over MySQL JSON semantics.


def normalized_type(value: str, dialect: str) -> str:
    name = value.lower().split("(", 1)[0].strip()
    if dialect == "mysql":
        # INFORMATION_SCHEMA.DATA_TYPE omits these modifiers, but callers may
        # supply COLUMN_TYPE. They never make a user-defined type trusted.
        words = name.split()
        if words and all(w in {"unsigned", "signed", "zerofill"} for w in words[1:]):
            name = words[0]
        return _MYSQL_TYPES.get(name, name)
    return name
