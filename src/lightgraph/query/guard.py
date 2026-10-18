"""Strict parsing and authorization for compiler-produced read-only SQL.

The guard is a second boundary, not a substitute for the database role. Unknown
syntax and custom functions fail closed; catalog types must be trusted built-ins.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Iterable
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope
from lightgraph.contracts import TableSpec
from lightgraph.errors import LightGraphError

SAFE_TYPES = frozenset({"text", "varchar", "character varying", "character", "char", "nvarchar", "string", "integer", "int", "int2", "int4", "int8", "smallint", "bigint", "numeric", "decimal", "real", "double precision", "float", "float4", "float8", "boolean", "bool", "date", "timestamp", "timestamp without time zone", "timestamp with time zone", "timestamptz", "time", "blob", "bytea"})
SAFE_NODES = frozenset({"Select", "Union", "Intersect", "Subquery", "Table", "TableAlias", "Identifier", "Column", "Star", "From", "Join", "Where", "Group", "Having", "Order", "Ordered", "Limit", "Offset", "Distinct", "Alias", "Paren", "And", "Or", "Not", "EQ", "NEQ", "GT", "GTE", "LT", "LTE", "Is", "In", "Between", "Null", "Boolean", "Literal", "Placeholder", "Parameter", "Case", "If", "Count", "Sum", "Min", "Max", "Length", "Coalesce", "Abs", "Cast", "DataType", "DataTypeParam", "Add", "Sub", "Mul", "Div", "Neg", "With", "CTE", "Exists", "D Pipe".replace(" ", ""), "DPipe", "Tuple", "Filter", "Like", "Escape"})

@dataclass(frozen=True, repr=False)
class ValidatedQuery:
    sql: str = field(repr=False)
    parameters: dict[str, Any] = field(default_factory=dict, repr=False)
    operator: str = "select"
    result_columns: tuple[str, ...] = ()
    scan_estimate: int | None = None
    def __repr__(self) -> str:
        return f"ValidatedQuery(operator={self.operator!r}, parameters={len(self.parameters)})"
    def __reduce_ex__(self, protocol):
        raise LightGraphError("ephemeral_serialization_forbidden")

class SQLGuard:
    def __init__(self, tables: Iterable[TableSpec], *, dialect: str = "postgres"):
        self.tables = {t.qualified_name: t for t in tables}
        self.dialect = dialect

    def validate(self, sql: str, parameters: dict[str, Any] | None = None, *, operator: str = "select", result_columns: Iterable[str] = ()) -> ValidatedQuery:
        try:
            statements = sqlglot.parse(sql, read=self.dialect, error_level=sqlglot.ErrorLevel.RAISE)
            if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.Union, exp.Intersect)):
                raise ValueError
            tree = statements[0]
            for node in tree.walk():
                if type(node).__name__ not in SAFE_NODES:
                    raise ValueError
                if isinstance(node, exp.With) and node.args.get("recursive"):
                    raise ValueError
                if isinstance(node, exp.Star) and not isinstance(node.parent, exp.Count):
                    raise LightGraphError("wildcard_projection_forbidden")
                if isinstance(node, exp.Join):
                    on = node.args.get("on")
                    if node.args.get("kind") == "CROSS" or on is None:
                        raise ValueError
                    parts = list(on.flatten()) if isinstance(on, exp.And) else [on]
                    if not parts or any(not isinstance(part, exp.EQ) or
                        not isinstance(part.left, exp.Column) or not isinstance(part.right, exp.Column) or
                        not part.left.table or not part.right.table or part.left.table == part.right.table
                        for part in parts):
                        raise LightGraphError("unbounded_join_forbidden")
                if isinstance(node, exp.Offset):
                    raise ValueError
                if isinstance(node, exp.Cast) and node.args["to"].sql().lower() not in SAFE_TYPES:
                    raise ValueError
                if isinstance(node, exp.Placeholder) and node.name not in (parameters or {}):
                    raise ValueError
            self._authorize(tree)
            return ValidatedQuery(sql, dict(parameters or {}), operator, tuple(result_columns))
        except LightGraphError:
            raise
        except Exception:
            raise LightGraphError("unsafe_sql") from None

    def _authorize(self, tree: exp.Expression) -> None:
        validated_joins = set()
        for scope in traverse_scope(tree):
            from_clause = scope.expression.args.get("from_") or scope.expression.args.get("from")
            bound = {from_clause.this.alias_or_name} if from_clause is not None else set()
            for join in scope.expression.args.get("joins", []):
                alias = join.this.alias_or_name
                if not alias or alias in bound:
                    raise LightGraphError("invalid_join_alias")
                on = join.args.get("on")
                parts = list(on.flatten()) if isinstance(on, exp.And) else [on]
                connected = False
                for part in parts:
                    if not isinstance(part, exp.EQ) or not isinstance(part.left, exp.Column) or not isinstance(part.right, exp.Column):
                        raise LightGraphError("unbounded_join_forbidden")
                    left, right = part.left.table, part.right.table
                    if {left, right} - (bound | {alias}):
                        raise LightGraphError("unbound_join_alias")
                    connected |= (left == alias and right in bound) or (right == alias and left in bound)
                if not connected:
                    raise LightGraphError("unbounded_join_forbidden")
                bound.add(alias)
                validated_joins.add(id(join))
            for alias, (_, source) in scope.selected_sources.items():
                if isinstance(source, exp.Table):
                    if source.catalog:
                        raise LightGraphError("unauthorized_object")
                    name = f"{source.db}.{source.name}" if source.db else source.name
                    if name not in self.tables:
                        raise LightGraphError("unauthorized_object")
                    # Type checks apply to referenced columns below. An unrelated
                    # custom-type column must not disable safe text projections.
            for column in scope.columns:
                if column.is_star:
                    raise LightGraphError("wildcard_projection_forbidden")
                matches = []
                for alias, (_, source) in scope.selected_sources.items():
                    if column.table and column.table != alias:
                        continue
                    if isinstance(source, exp.Table):
                        name = f"{source.db}.{source.name}" if source.db else source.name
                        selected = next((c for c in self.tables[name].columns if c.name == column.name), None)
                        # Uploaded cells may have heterogeneous primitive types.
                        # SQLite can project them without invoking a user type;
                        # comparisons are separately rejected by Predicate/1.0.
                        declared_type = selected.data_type.lower().split("(")[0].strip() if selected else None
                        if selected and declared_type not in SAFE_TYPES and not (self.dialect == "sqlite" and declared_type == "mixed"):
                            raise LightGraphError("untrusted_column_type")
                        matches.append(selected is not None)
                    else:
                        matches.append(column.name in source.expression.named_selects)
                if not any(matches):
                    # Output aliases are already resolved/excluded by Scope;
                    # never treat an arbitrary selected name as authorization.
                    raise LightGraphError("unauthorized_column")
                if not column.table and sum(matches) > 1:
                    raise LightGraphError("ambiguous_column")
            for star in scope.stars:
                # COUNT(*) is the only admitted wildcard; SELECT * exposes columns.
                if not isinstance(star.parent, exp.Count):
                    raise LightGraphError("wildcard_projection_forbidden")

        if any(id(join) not in validated_joins for join in tree.find_all(exp.Join)):
            raise LightGraphError("unsupported_join_shape")
