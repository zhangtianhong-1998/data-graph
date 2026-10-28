"""Strict parsing and authorization for compiler-produced read-only SQL.

The guard is a second boundary, not a substitute for the database role. Unknown
syntax and custom functions fail closed; catalog types must be trusted built-ins.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable, Mapping
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope
from lightgraph.contracts import TableSpec
from lightgraph.errors import LightGraphError
from .dialects import normalized_type

SAFE_TYPES = frozenset({"text", "varchar", "character varying", "character", "char", "nvarchar", "string", "integer", "int", "int2", "int4", "int8", "smallint", "bigint", "numeric", "decimal", "real", "double precision", "float", "float4", "float8", "boolean", "bool", "date", "timestamp", "timestamp without time zone", "timestamp with time zone", "timestamptz", "time", "blob", "bytea"})
SAFE_NODES = frozenset({"Select", "Union", "Intersect", "Subquery", "Table", "TableAlias", "Identifier", "Column", "Star", "From", "Join", "Where", "Group", "Having", "Order", "Ordered", "Limit", "Offset", "Distinct", "Alias", "Paren", "And", "Or", "Not", "EQ", "NEQ", "GT", "GTE", "LT", "LTE", "Is", "In", "Between", "Null", "Boolean", "Literal", "Placeholder", "Parameter", "Case", "If", "Count", "Sum", "Min", "Max", "Length", "Coalesce", "Abs", "Cast", "DataType", "DataTypeParam", "Add", "Sub", "Mul", "Div", "Neg", "With", "CTE", "Exists", "D Pipe".replace(" ", ""), "DPipe", "Tuple", "Filter", "Like", "Escape"})


@lru_cache(maxsize=16)
def _strict_parser_type(parser_type):
    """Reject SQLGlot's command fallback before it logs the source SQL.

    ErrorLevel.RAISE controls parse errors, but not unsupported-command warnings.
    A per-dialect subclass avoids changing process-wide logging or parser state.
    """
    class StrictParser(parser_type):
        def _warn_unsupported(self):
            raise ValueError("unsupported_sql")

    return StrictParser

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
    def __init__(self, tables: Iterable[TableSpec], *, dialect: str = "postgres",
                 physical_columns: Mapping[str, Iterable[str]] | None = None):
        self.tables = {t.qualified_name: t for t in tables}
        self.dialect = dialect
        # Kept separate from the authorized projection. GROUP BY resolves an
        # input column before a SELECT alias, including columns hidden by the
        # application allowlist. An incomplete catalog cannot prove no clash.
        self.physical_columns = {name: frozenset(columns) for name, columns in
                                 (physical_columns or {}).items() if name in self.tables}

    def _output_alias(self, column, scope):
        if column.table or scope.expression.named_selects.count(column.name) != 1:
            return False
        parent = column.parent
        if (isinstance(parent, exp.Ordered) and parent.this is column
                and isinstance(parent.parent, exp.Order)
                and parent.parent.parent is scope.expression):
            # Bare ORDER BY output aliases bind the already-authorized result,
            # including compound SELECT output. Expressions such as LENGTH(x)
            # do not inherit this exception.
            return True
        if not (isinstance(parent, exp.Group) and parent.parent is scope.expression):
            return False
        for _, source in scope.selected_sources.values():
            if isinstance(source, exp.Table):
                name = f"{source.db}.{source.name}" if source.db else source.name
                physical = self.physical_columns.get(name)
                # SQLite/MySQL resolve column names without case, and Postgres
                # folds unquoted names. A case variant of a hidden input must
                # never become an apparent output alias. Conservatively cover
                # quoted variants as well rather than infer server collation.
                if physical is None or column.name.casefold() in {name.casefold() for name in physical}:
                    return False
            elif column.name.casefold() in {name.casefold() for name in
                                           (source.outer_columns or source.expression.named_selects)}:
                return False
        return True

    def validate(self, sql: str, parameters: dict[str, Any] | None = None, *, operator: str = "select", result_columns: Iterable[str] = ()) -> ValidatedQuery:
        try:
            dialect = sqlglot.Dialect.get_or_raise(self.dialect)
            tokens = dialect.tokenize(sql)
            if self.dialect == "mysql":
                # MySQL executes version comments that a generic SQL parser
                # treats as inert text. Hints may also alter session settings.
                for token in tokens:
                    if (token.token_type == sqlglot.tokens.TokenType.HINT or
                            any(comment.lstrip().upper().startswith(("!", "M!"))
                                for comment in token.comments)):
                        raise LightGraphError("unsafe_sql")
            parser = _strict_parser_type(dialect.parser_class)(
                dialect=dialect, error_level=sqlglot.ErrorLevel.RAISE,
            )
            statements = parser.parse(tokens, sql)
            if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.Union, exp.Intersect)):
                raise ValueError
            tree = statements[0]
            for node in tree.walk():
                if type(node).__name__ not in SAFE_NODES:
                    raise ValueError
                if self.dialect == "mysql" and isinstance(node, exp.Parameter):
                    # @user variables can read/write mutable connection state;
                    # they are not the compiler's named parameter placeholders.
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
        validated_columns = set()
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
                    if self.dialect == "mysql" and source.db.casefold() in {
                            "mysql", "information_schema", "performance_schema", "sys"}:
                        raise LightGraphError("unauthorized_object")
                    name = f"{source.db}.{source.name}" if source.db else source.name
                    if name not in self.tables:
                        raise LightGraphError("unauthorized_object")
                    # Type checks apply to referenced columns below. An unrelated
                    # custom-type column must not disable safe text projections.
            # Scalar-subquery scopes can propagate unqualified child columns as
            # apparent external references before SQLGlot resolves names. Child
            # scopes are visited first, so skip only the exact AST nodes already
            # authorized there. A genuinely unbound/correlated child reference
            # still fails its own authorization; names alone never grant reuse.
            own_columns = list(scope.find_all(exp.Column))
            columns = [column for column in scope.columns if id(column) not in validated_columns
                       and not self._output_alias(column, scope)]
            checked_ids = {id(column) for column in columns}
            for column in own_columns:
                if id(column) in checked_ids:
                    continue
                # sqlglot's optimizer view omits unqualified HAVING columns and
                # apparent output aliases inside ORDER/DISTINCT expressions.
                # Those expressions can bind physical input columns instead;
                # authorization must inspect them even when optimization does
                # not. Bare ORDER/GROUP aliases need the scope-specific checks
                # above. HAVING aliases must repeat their expression (or use an
                # outer SELECT) in this SQL subset.
                if not self._output_alias(column, scope):
                    columns.append(column)
            for column in columns:
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
                        declared_type = normalized_type(selected.data_type, self.dialect) if selected else None
                        if selected and declared_type not in SAFE_TYPES and not (
                                self.dialect == "sqlite" and declared_type == "mixed" or
                                self.dialect == "mysql" and declared_type == "json"):
                            raise LightGraphError("untrusted_column_type")
                        matches.append(selected is not None)
                    else:
                        # A CTE/derived-table column list renames the inner
                        # projection. Its source columns were authorized in
                        # the child scope; only the exposed names are visible
                        # here. Ignoring this list rejects ordinary unpivot
                        # queries such as WITH items(code) AS (SELECT l1_code…).
                        exposed = source.outer_columns or source.expression.named_selects
                        matches.append(column.name in exposed)
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
            validated_columns.update(id(column) for column in own_columns)

        if any(id(join) not in validated_joins for join in tree.find_all(exp.Join)):
            raise LightGraphError("unsupported_join_shape")
