"""Compile bounded, catalog-bound operators. Values are always driver parameters."""
from __future__ import annotations
from typing import Any, Iterable, Mapping
import re
import sqlglot
from sqlglot import exp
from sqlglot.dialects.mysql import MySQL
from sqlglot.dialects.sqlite import SQLite
from lightgraph.contracts import TableSpec
from lightgraph.errors import LightGraphError
from .guard import SQLGuard, ValidatedQuery

def ident(value: str, dialect: str = "postgres") -> str:
    quote = "`" if dialect == "mysql" else '"'
    return quote + value.replace(quote, quote + quote) + quote


def render_select(tree: exp.Expression, dialect: str) -> str:
    """Render a validated query without dropping SQLite's CTE column names.

    SQLGlot disables ordinary derived-table column aliases for SQLite, but its
    generic TableAlias renderer also strips supported WITH name(columns) lists.
    Preserve that specific syntax when narrowing a model query's result limit.
    """
    if dialect != "sqlite":
        return tree.sql(dialect=dialect)

    class CTEColumnsGenerator(SQLite.Generator):
        def tablealias_sql(self, expression):
            if isinstance(expression.parent, exp.CTE):
                alias = self.sql(expression, "this")
                columns = self.expressions(expression, key="columns", flat=True)
                return f"{alias}({columns})" if columns else alias
            return super().tablealias_sql(expression)

    return CTEColumnsGenerator(dialect="sqlite").generate(tree)


def mysql_driver_sql(query: ValidatedQuery) -> str:
    """Render named binds for PyMySQL without interpolating source values.

    MySQL's SQLGlot generator retains ``:name``. Converting an entire SQL string
    with a regex would mistake identifiers or literals for bind parameters.
    This generator changes only placeholder AST nodes and escapes percent signs
    in data-bearing SQL tokens for PyMySQL's pyformat protocol.
    """
    class DriverGenerator(MySQL.Generator):
        def placeholder_sql(self, expression):
            name = expression.name
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name) or name not in query.parameters:
                raise LightGraphError("unsafe_sql")
            return "%(" + name + ")s"

        def identifier_sql(self, expression):
            return super().identifier_sql(expression).replace("%", "%%")

        def literal_sql(self, expression):
            return super().literal_sql(expression).replace("%", "%%")

    try:
        tree = sqlglot.parse_one(query.sql, read="mysql")
        return DriverGenerator(dialect="mysql", identify=True, comments=False).generate(tree)
    except LightGraphError:
        raise
    except Exception:
        raise LightGraphError("unsafe_sql") from None

class QueryCompiler:
    def __init__(self, tables: Iterable[TableSpec], *, dialect: str = "postgres",
                 physical_columns: Mapping[str, Iterable[str]] | None = None):
        self.tables = {t.qualified_name: t for t in tables}
        self.guard = SQLGuard(self.tables.values(), dialect=dialect, physical_columns=physical_columns)
        self.dialect = dialect

    def ident(self, value: str) -> str:
        return ident(value, self.dialect)

    def table(self, table: TableSpec) -> str:
        stored = self.tables.get(table.qualified_name)
        if stored is None or stored.source_id != table.source_id:
            raise LightGraphError("unauthorized_object")
        return f"{self.ident(table.schema_name)}.{self.ident(table.name)}"

    def columns(self, table: TableSpec, columns: Iterable[str], alias: str | None = None) -> list[str]:
        self.table(table)
        columns = list(columns)
        if not columns or len(columns) != len(set(columns)):
            raise LightGraphError("invalid_column_selection")
        if not set(columns) <= set(self.tables[table.qualified_name].column_names):
            raise LightGraphError("unauthorized_column")
        return [(self.ident(alias) + "." if alias else "") + self.ident(c) for c in columns]

    def predicate(self, table: TableSpec, predicate: dict | None, params: dict[str, Any]) -> str:
        from .predicate import compile_predicate
        self.table(table)
        registered=self.tables[table.qualified_name]
        return compile_predicate(registered,predicate,params,
            column_sql=lambda name:self.columns(registered,[name])[0],dialect=self.dialect)

    def finish(self, sql: str, params: dict, op: str, columns: Iterable[str] = ()) -> ValidatedQuery:
        query = self.guard.validate(sql, params, operator=op, result_columns=columns)
        if self.dialect == "mysql":
            # Quote compiler-owned aliases such as ROWS, a MySQL keyword. Parse
            # only after the strict guard has ruled out command fallbacks.
            sql = sqlglot.parse_one(query.sql, read="mysql").sql(dialect="mysql", identify=True)
            return self.guard.validate(sql, params, operator=op, result_columns=query.result_columns)
        return query

    def projection(self, table: TableSpec, columns: list[str], *, predicate: dict | None = None, distinct: bool = True, limit: int = 20000) -> ValidatedQuery:
        if not isinstance(limit, int) or not 1 <= limit <= 10_000_000:
            raise LightGraphError("invalid_result_limit")
        cols, params = self.columns(table, columns), {}
        where = self.predicate(table, predicate, params)
        # One look-ahead row detects incomplete partitions; adapters reject it.
        params["cap"] = limit + 1
        sql = f"SELECT {'DISTINCT ' if distinct else ''}{', '.join(cols)} FROM {self.table(table)} WHERE {where} LIMIT :cap"
        return self.finish(sql, params, "distinct_projection" if distinct else "projection", columns)

    def profile_column(self, table: TableSpec, column: str, predicate=None) -> ValidatedQuery:
        c, params = self.columns(table, [column])[0], {}
        where = self.predicate(table, predicate, params)
        text = f"CAST({c} AS {'CHAR' if self.dialect == 'mysql' else 'TEXT'})"
        length = "CHAR_LENGTH" if self.dialect == "mysql" else "LENGTH"
        sql = f"""SELECT COUNT(*) AS rows, COUNT({c}) AS non_null,
            COUNT(DISTINCT {c}) AS distinct_exact,
            SUM(CASE WHEN {c} IS NULL THEN 1 ELSE 0 END) AS nulls,
            SUM(CASE WHEN {text} = '' THEN 1 ELSE 0 END) AS empty_str,
            MIN({length}({text})) AS min_len, MAX({length}({text})) AS max_len
            FROM {self.table(table)} WHERE {where}"""
        return self.finish(sql, params, "profile_column")

    def profile_column_group(self, table: TableSpec, columns: list[str], predicate=None) -> ValidatedQuery:
        cols, params = self.columns(table, columns), {}
        where = self.predicate(table, predicate, params)
        nulls = " OR ".join(c + " IS NULL" for c in cols)
        sql = f"""SELECT COUNT(*) AS distinct_tuples, COALESCE(SUM(freq), 0) AS rows,
          COALESCE(SUM(CASE WHEN freq > 1 THEN 1 ELSE 0 END), 0) AS duplicate_groups,
          COALESCE(MAX(freq), 0) AS max_duplicate,
          COALESCE(SUM(CASE WHEN missing > 0 THEN freq ELSE 0 END), 0) AS null_key_rows
          FROM (SELECT COUNT(*) AS freq,
            MAX(CASE WHEN {nulls} THEN 1 ELSE 0 END) AS missing
            FROM {self.table(table)} WHERE {where} GROUP BY {', '.join(cols)}) g"""
        return self.finish(sql, params, "profile_column_group")

    def check_fd(self, table: TableSpec, keys: list[str], value: str, predicate=None) -> ValidatedQuery:
        cols, params = self.columns(table, keys), {}
        v = self.columns(table, [value])[0]
        where = self.predicate(table, predicate, params)
        complete = " AND ".join(c + " IS NOT NULL" for c in cols)
        # Null determinant rows remain separately observable by profile_group.
        sql = f"""SELECT COUNT(*) AS x_groups,
          COALESCE(SUM(CASE WHEN c > 1 THEN 1 ELSE 0 END), 0) AS violating_groups,
          COALESCE(SUM(CASE WHEN c > 1 THEN c - 1 ELSE 0 END), 0) AS excess_values,
          COALESCE(MAX(c), 0) AS max_values_per_x,
          COALESCE(SUM(CASE WHEN c = 0 THEN 1 ELSE 0 END), 0) AS all_null_groups,
          COALESCE(SUM(CASE WHEN c > 0 AND n > 0 THEN 1 ELSE 0 END), 0) AS mixed_null_groups,
          COALESCE(SUM(n), 0) AS null_attribute_rows
          FROM (SELECT COUNT(DISTINCT {v}) AS c,
            SUM(CASE WHEN {v} IS NULL THEN 1 ELSE 0 END) AS n
            FROM {self.table(table)} WHERE ({where}) AND {complete}
            GROUP BY {', '.join(cols)}) g"""
        return self.finish(sql, params, "check_fd")

    def check_value_domain(self, table: TableSpec, column: str, allowed: list[Any], predicate=None) -> ValidatedQuery:
        if len(allowed) > 64:
            raise LightGraphError("domain_limit_exceeded")
        c, params = self.columns(table, [column])[0], {}
        where = self.predicate(table, predicate, params)
        binds = []
        for v in allowed:
            if not isinstance(v, (str, int, float, bool, type(None))):
                raise LightGraphError("invalid_domain")
            if v is not None:
                key = f"p{len(params)}"; params[key] = v; binds.append(":" + key)
        invalid = f"{c} NOT IN ({', '.join(binds)})" if binds else f"{c} IS NOT NULL"
        sql = f"""SELECT COUNT(*) AS rows, COUNT(DISTINCT {c}) AS distinct_exact,
          COALESCE(SUM(CASE WHEN {invalid} THEN 1 ELSE 0 END), 0) AS out_of_domain,
          COALESCE(SUM(CASE WHEN {c} IS NULL THEN 1 ELSE 0 END), 0) AS nulls
          FROM {self.table(table)} WHERE {where}"""
        return self.finish(sql, params, "check_value_domain")

    def check_reference(self, source: TableSpec, source_columns: list[str], target: TableSpec, target_columns: list[str], *, discriminator_columns: list[str] | None = None, predicate=None, target_predicate=None) -> ValidatedQuery:
        if len(source_columns) != len(target_columns):
            raise LightGraphError("reference_arity_mismatch")
        sc = self.columns(source, source_columns); tc = self.columns(target, target_columns)
        dc = self.columns(target, discriminator_columns) if discriminator_columns else []
        params = {}; sw = self.predicate(source, predicate, params); tw = self.predicate(target, target_predicate, params)
        # Target DISTINCT removes fact duplication; only semantic discriminator
        # conflicts can create ambiguity. Scope must be part of both key lists.
        complete = " AND ".join(c + " IS NOT NULL" for c in tc)
        bindings = ", ".join(dict.fromkeys(tc + dc))
        missing = " OR ".join(f"s.{c} IS NULL" for c in sc)
        populated = " OR ".join(f"s.{c} IS NOT NULL" for c in sc)
        join = " AND ".join(f"s.{a} = t.{b}" for a, b in zip(sc, tc))
        binding_alias = "_lg_bindings"
        while binding_alias in target_columns: binding_alias = "_" + binding_alias
        binding_count = "t." + self.ident(binding_alias)
        sql = f"""SELECT COUNT(*) AS source_ref_tuples,
          COALESCE(SUM(CASE WHEN {missing} THEN 1 ELSE 0 END), 0) AS null_refs,
          COALESCE(SUM(CASE WHEN ({missing}) AND ({populated}) THEN 1 ELSE 0 END), 0) AS partial_null_refs,
          COALESCE(SUM(CASE WHEN NOT ({missing}) AND {binding_count} IS NULL THEN 1 ELSE 0 END), 0) AS unmatched,
          COALESCE(SUM(CASE WHEN {binding_count} > 1 THEN 1 ELSE 0 END), 0) AS ambiguous,
          COALESCE(SUM(CASE WHEN {binding_count} = 1 THEN 1 ELSE 0 END), 0) AS unique_matches
          FROM (SELECT DISTINCT {', '.join(sc)} FROM {self.table(source)} WHERE {sw}) s
          LEFT JOIN (SELECT {', '.join(tc)}, COUNT(*) AS {self.ident(binding_alias)}
            FROM (SELECT DISTINCT {bindings} FROM {self.table(target)} WHERE ({tw}) AND {complete}) b
            GROUP BY {', '.join(tc)}) t ON {join}"""
        return self.finish(sql, params, "check_reference")

    def path_probe(self, table: TableSpec, columns: list[str], predicate=None) -> ValidatedQuery:
        cols, params = self.columns(table, columns), {}
        if len(cols) > 32: raise LightGraphError("hierarchy_depth_exceeded")
        where = self.predicate(table, predicate, params)
        terms = ["COUNT(*) AS rows"]
        terms.extend(f"COALESCE(SUM(CASE WHEN {c} IS NULL THEN 1 ELSE 0 END), 0) AS null_{i}" for i, c in enumerate(cols))
        terms.extend(f"COALESCE(SUM(CASE WHEN {a} = {b} THEN 1 ELSE 0 END), 0) AS repeat_{i + 1}" for i, (a, b) in enumerate(zip(cols, cols[1:])))
        terms.extend(f"COALESCE(SUM(CASE WHEN {a} IS NOT NULL AND {b} IS NULL AND {c} IS NOT NULL THEN 1 ELSE 0 END), 0) AS gap_{i + 1}" for i, (a,b,c) in enumerate(zip(cols,cols[1:],cols[2:])))
        return self.finish(f"SELECT {', '.join(terms)} FROM {self.table(table)} WHERE {where}", params, "path_probe")

    def unpivot_probe(self, table: TableSpec, levels: list[dict], predicate=None, limit=20000) -> ValidatedQuery:
        if not 1 <= len(levels) <= 32: raise LightGraphError("hierarchy_depth_exceeded")
        params, selects = {}, []
        where = self.predicate(table, predicate, params)
        for i, level in enumerate(levels):
            if set(level) - {"code", "label", "level"}: raise LightGraphError("invalid_level")
            code = self.columns(table, [level["code"]])[0]
            label = self.columns(table, [level["label"]])[0] if level.get("label") else "NULL"
            selects.append(f"SELECT DISTINCT {i + 1} AS level_position, {code} AS code, {label} AS label FROM {self.table(table)} WHERE ({where}) AND {code} IS NOT NULL")
        params["cap"] = limit + 1
        return self.finish(" UNION ALL ".join(selects) + " LIMIT :cap", params, "unpivot_probe", ["level_position", "code", "label"])

    def compare_attribute_role(self, a: TableSpec, ca: str, b: TableSpec, cb: str, predicate=None, target_predicate=None) -> ValidatedQuery:
        ac,bc=self.columns(a,[ca])[0],self.columns(b,[cb])[0]
        params={}; aw=self.predicate(a,predicate,params);bw=self.predicate(b,target_predicate,params)
        if self.dialect == "mysql":
            # INTERSECT is unavailable on MySQL releases before 8.0.31. The
            # distinct equijoin preserves set-overlap counts without a product.
            sql = f"""SELECT COUNT(*) AS overlap_values
              FROM (SELECT DISTINCT {ac} AS v FROM {self.table(a)} WHERE ({aw}) AND {ac} IS NOT NULL) l
              INNER JOIN (SELECT DISTINCT {bc} AS v FROM {self.table(b)} WHERE ({bw}) AND {bc} IS NOT NULL) r
              ON l.v = r.v"""
        else:
            sql=f"SELECT COUNT(*) AS overlap_values FROM (SELECT DISTINCT {ac} AS v FROM {self.table(a)} WHERE ({aw}) AND {ac} IS NOT NULL INTERSECT SELECT DISTINCT {bc} AS v FROM {self.table(b)} WHERE ({bw}) AND {bc} IS NOT NULL) i"
        return self.finish(sql, params, "compare_attribute_role")
