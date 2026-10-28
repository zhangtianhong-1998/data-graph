"""Bounded discovery tools over the source adapter's guarded execution boundary.

Returned observations are ephemeral model context, never publication evidence or
checkpoint content. The executor borrows its adapter and never accesses a driver
connection. A goal narrows available objects; it is not a row authorization policy.
Database roles, restricted views or RLS must enforce any required row isolation.
"""
from __future__ import annotations

import math
import re
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from lightgraph.contracts import ExtractionGoal, SourceRef, TableSpec, canonical, digest
from lightgraph.documents import DocumentCorpus
from lightgraph.context import document_reference
from lightgraph.errors import BudgetExceeded, LightGraphError
from lightgraph.query import QueryCompiler
from lightgraph.query.compiler import ident, render_select
from lightgraph.sources.base import aggregate_count
from .budget import BudgetAllocator, Cost
from .goal import column_is_excluded, source_in_goal


_SENSITIVE = re.compile(r"password|secret|token|private|phone|email|identity_card|id_card|身份证|手机号|密码|密钥", re.I)
_SAFE_ERRORS = frozenset({
    "unsafe_sql", "wildcard_projection_forbidden", "unbounded_join_forbidden",
    "invalid_join_alias", "unbound_join_alias", "unsupported_join_shape",
    "unauthorized_object", "unauthorized_column", "ambiguous_column",
    "untrusted_column_type", "invalid_column_selection", "reference_arity_mismatch",
    "invalid_tool_arguments", "unknown_tool", "metadata_only_query_forbidden",
    "executor_closed", "invalid_result_limit", "invalid_aggregate_shape",
    "memory_budget_exhausted", "query_timeout", "query_cancelled",
    "query_execution_failed", "query_budget_exhausted", "run_time_budget_exhausted",
    "returned_rows_budget_exhausted", "returned_bytes_budget_exhausted",
    "db_cu_budget_exhausted", "wall_time_budget_exhausted", "source_closed",
    "source_value_type_unsupported", "non_finite_source_value", "result_truncated",
    "formula_cache_missing_in_projection",
    "document_context_too_large", "stale_document_reference", "document_reference_scope_mismatch",
})
_STRING = {"type": "string", "minLength": 1}
_COLUMNS = {"type": "array", "items": _STRING, "minItems": 1, "maxItems": 32}


def _function(name: str, description: str, properties: dict, required=()) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": list(required), "additionalProperties": False}}}


class ToolExecutor:
    """Provide one execution boundary for SQL and common discovery operations.

    Callers must discard tool results after the run and must not put observations
    in logs, tracing exporters, checkpoints or exception messages. Only the
    explicitly numerical ``aggregates`` field is suitable for probe summaries.
    """

    def __init__(self, adapter, goal: ExtractionGoal, budget: BudgetAllocator,
                 documents=None, allow_source_samples: bool = False, allowed_tables=None,
                 source_adapters=None):
        self.adapter = adapter
        self.goal = goal
        self.budget = budget
        self.allow_source_samples = allow_source_samples
        self.documents = documents if isinstance(documents, DocumentCorpus) else DocumentCorpus(documents or ())
        self._closed = False
        self._sequence = 0
        self._reserved_probe_ids = set()
        self._max_rows = min(128, adapter.limits.max_result_rows)
        self._max_bytes = min(65536, adapter.limits.max_result_bytes, budget.limits.memory_bytes)
        self.source_adapters=dict(source_adapters or {})
        local_catalog=list(adapter.describe())
        self._local_source_ids={t.source_id for t in local_catalog}
        for source_id in self._local_source_ids:
            self.source_adapters[source_id]=adapter
        catalog={}
        for current in self.source_adapters.values():
            for table in current.describe():
                catalog[(table.source_id,table.qualified_name)]=table
        if self.documents._catalog is None:
            self.documents.bind_catalog(catalog.values())
        self._context_tables={}
        self._context_refs=[]
        allowed = None if allowed_tables is None else {
            f'{t.source_id}:{t.qualified_name}' if isinstance(t, TableSpec) else t for t in allowed_tables}
        tables = []
        for table in catalog.values():
            matches = {table.source_id, table.schema_name, table.name, table.qualified_name,
                       f"{table.source_id}.{table.schema_name}", f"{table.source_id}.{table.qualified_name}",
                       f"{table.source_id}:{table.qualified_name}"}
            if not source_in_goal(goal, table):
                continue
            if allowed is not None and not matches.intersection(allowed):
                continue
            columns = [column for column in table.columns if not _SENSITIVE.search(column.name)
                       and not column_is_excluded(goal, table, column.name)]
            if columns:
                filtered = table.model_copy(deep=True)
                filtered.columns = columns
                filtered.unique_keys = [key for key in table.unique_keys if set(key) <= set(filtered.column_names)]
                tables.append(filtered)
        self.tables = {f'{table.source_id}:{table.qualified_name}': table for table in tables}
        self._table_aliases={}
        for table in tables:
            key=f'{table.source_id}:{table.qualified_name}'
            self._table_aliases.setdefault(table.qualified_name,[]).append(key)
            self._table_aliases[f'{table.source_id}.{table.qualified_name}']=[key]
        # A foreign key must not reveal an object hidden from this tool's catalog.
        for table in tables:
            table.foreign_keys = [fk for fk in table.foreign_keys
                                 if set(fk.columns) <= set(table.column_names)
                                 and (target := self.tables.get(f"{table.source_id}:{fk.target_schema}.{fk.target_table}")) is not None
                                 and set(fk.target_columns) <= set(target.column_names)]
        self.compiler = QueryCompiler([t for t in tables if t.source_id in self._local_source_ids],
            dialect=adapter.compiler.dialect, physical_columns=adapter.compiler.guard.physical_columns)

    def begin_context(self, tables=()):
        """Start a new task with fresh scope and no previous observations/references."""
        self._context_tables.clear()
        self._context_refs.clear()
        self._activate_tables(tables)

    def _activate_tables(self, tables):
        for table in tables:
            if isinstance(table, SourceRef):
                table=self._table(f'{table.source_id}:{table.schema_name}.{table.table}')
            elif isinstance(table,str):
                table=self._table(table)
            key=f'{table.source_id}:{table.qualified_name}'
            if key not in self.tables:
                raise LightGraphError('unauthorized_object')
            names=set(table.column_names)&set(self.tables[key].column_names)
            previous=self._context_tables.get(key)
            if previous:
                names|=set(previous.column_names)
            table=self.tables[key].model_copy(update={'columns':[c for c in self.tables[key].columns if c.name in names]})
            self._context_tables[key]=table
        sections=self.documents.contexts_for(self._context_tables.values(),max_bytes=min(12000,self._max_bytes//2))
        self._context_refs=[document_reference(s) for s in sections]

    def context_tables(self):
        return [SourceRef(source_id=t.source_id,schema=t.schema_name,table=t.name).model_dump(mode='json',by_alias=True)
                for t in self._context_tables.values()]

    def document_refs(self):
        """Safe issued-reference registry; raw observations are never included."""
        self.context_documents()
        return [dict(ref) for ref in self._context_refs]

    def context_documents(self):
        """Usable after close; revalidate every registered revision and scope."""
        return self.documents.validate_references(self._context_refs,list(self._context_tables.values()))

    def _activate_query(self, sql):
        tree=sqlglot.parse_one(sql,read=self.compiler.dialect)
        involved=[]
        for scope in traverse_scope(tree):
            for alias,(_,source) in scope.selected_sources.items():
                if not isinstance(source,exp.Table):
                    continue
                name=f'{source.db}.{source.name}' if source.db else source.name
                table=self.compiler.tables.get(name)
                if table is None:
                    raise LightGraphError('unauthorized_object')
                names={c.name for c in scope.columns if (c.table==alias or not c.table) and c.name in table.column_names}
                involved.append(table.model_copy(update={'columns':[c for c in table.columns if c.name in names]}))
        self._activate_tables(involved)

    def __repr__(self):
        return f"ToolExecutor(tables={len(self.tables)}, closed={self._closed})"

    def __reduce_ex__(self, protocol):
        raise LightGraphError("ephemeral_serialization_forbidden")

    def tool_specs(self) -> list[dict]:
        return [
            _function("get_metadata", "Read authorized table declarations, tool_table_id, quoted sql_table/sql_identifier and stable column IDs. Use tool_table_id in table tool arguments, column.name in column tool arguments, quoted SQL identifiers in SQL, and column.id in proposal references. Declarations are hypotheses, not observed semantics.",
                      {"table": _STRING}),
            _function("search_knowledge", "Read an applicable document window. Use the returned next_page to read remaining context. Partial sections cannot independently confirm semantics. Use source_id:schema.table when names overlap.",
                      {"table": _STRING, "columns": _COLUMNS,
                       "page": {"type":"integer","minimum":0}}, ("table",)),
            _function("profile_columns", "Compute exact aggregate statistics for authorized columns. This may scan a full table; it does not infer business roles.",
                      {"table": _STRING, "columns": _COLUMNS}, ("table", "columns")),
            _function("check_fd", "Check key-to-attribute functional dependencies over the authorized table. Statistical correspondence is not semantic identity.",
                      {"table": _STRING, "keys": _COLUMNS, "values": _COLUMNS}, ("table", "keys", "values")),
            _function("check_reference", "Check distinct reference-key coverage between authorized tables, including bounded cross-source comparisons. Use source_id:schema.table for each endpoint. A match alone does not establish a business relationship.",
                      {"source_table": _STRING, "source_columns": _COLUMNS,
                       "target_table": _STRING, "target_columns": _COLUMNS},
                      ("source_table", "source_columns", "target_table", "target_columns")),
            _function("execute_readonly_sql", f"Execute a single guarded SELECT/CTE in the {self.compiler.dialect} dialect over authorized objects on this source. Copy the quoted sql_table and column sql_identifier from get_metadata; tool_table_id and opaque column.id are not SQL identifiers. Name columns explicitly and use named :parameters for values. No arbitrary functions, writes, cross joins or OFFSET. In HAVING repeat the aggregate expression or use an outer SELECT instead of a projection alias. To count distinct tuples use SELECT COUNT(*) FROM (SELECT DISTINCT col1,col2 FROM table) AS tuples; SQLite does not support COUNT(DISTINCT col1,col2) or COUNT(DISTINCT (col1,col2)). Cross-source comparisons use check_reference, not federated SQL. Rows are ephemeral and bounded; LIMIT does not bound scan cost. Tool results cannot authorize graph publication.",
                      {"sql": {"type": "string", "minLength": 1, "maxLength": 20000},
                       "parameters": {"type": "object", "additionalProperties": {"type": ["string", "number", "boolean", "null"]}},
                       "max_rows": {"type": "integer", "minimum": 1, "maximum": self._max_rows}}, ("sql",)),
        ]

    def reserve_probe_ids(self, identifiers):
        """A fresh Agent may still see earlier evidence in the same packet."""
        self._reserved_probe_ids.update(identifiers)

    def execute(self, name: str, arguments: dict[str, Any]) -> dict:
        """Return only bounded data or allowlisted error codes; never echo inputs."""
        self._sequence += 1
        probe_id = f"probe_{self._sequence:04d}"
        while probe_id in self._reserved_probe_ids:
            self._sequence += 1
            probe_id = f"probe_{self._sequence:04d}"
        names = {tool["function"]["name"] for tool in self.tool_specs()}
        operator = name if isinstance(name, str) and name in names else "unknown"
        try:
            if self._closed:
                raise LightGraphError("executor_closed")
            if not isinstance(name, str) or name not in names:
                raise LightGraphError("unknown_tool")
            if not isinstance(arguments, dict) or len(canonical(arguments).encode()) > 32768:
                raise LightGraphError("invalid_tool_arguments")
            spec = next(tool["function"]["parameters"] for tool in self.tool_specs()
                        if tool["function"]["name"] == name)
            if set(arguments) - set(spec["properties"]) or set(spec["required"]) - set(arguments):
                raise LightGraphError("invalid_tool_arguments")
            self.budget.check_deadline()
            if name not in {"get_metadata", "search_knowledge"} and not self.allow_source_samples:
                raise LightGraphError("metadata_only_query_forbidden")
            response = getattr(self, "_" + name)(**arguments)
            if self._context_tables:
                from lightgraph.context import bounded_document_window
                visible=bounded_document_window(self.context_documents(),max_bytes=min(12000,self._max_bytes//3))
                response['context']={'tables':self.context_tables(),'documents':visible,
                                     'document_refs':[document_reference(section) for section in visible]}
            response.update(status="ok", operator=operator, probe_id=probe_id)
            size = len(canonical(response).encode())
            if size > self._max_bytes:
                raise LightGraphError("memory_budget_exhausted")
            self.budget.charge(Cost(memory_bytes=size))
            return response
        except BudgetExceeded:
            # A hard resource boundary ends exploration. Returning a recoverable
            # tool error would invite another paid model turn after exhaustion.
            raise
        except Exception as error:
            code = error.code if isinstance(error, LightGraphError) and error.code in _SAFE_ERRORS else "tool_execution_failed"
            if isinstance(error, (TypeError, ValueError, KeyError)):
                code = "invalid_tool_arguments"
            result = {"status": "error", "operator": operator, "probe_id": probe_id, "code": code}
            if operator == "execute_readonly_sql" and code in {"unauthorized_object", "unauthorized_column", "ambiguous_column"}:
                # Fixed protocol help, never the rejected SQL, its identifiers,
                # or parameters. The narrowed metadata tool owns visibility.
                result["guidance"] = (
                    "Read get_metadata for the authorized sql_table and each column's sql_identifier. "
                    "Use those quoted names in SQL, not tool_table_id, source IDs or opaque column IDs. "
                    "Qualify physical tables with their schema and qualify ambiguous columns with a table alias. "
                    "For HAVING, repeat the aggregate expression or filter an outer SELECT. "
                    "Unknown objects remain forbidden; do not infer permission from this error."
                )
            elif code in {"unauthorized_object", "unauthorized_column", "ambiguous_column"}:
                result["guidance"] = (
                    "Call get_metadata with no table argument to list the authorized catalog. "
                    "Copy tool_table_id into table arguments and column.name into column arguments; "
                    "opaque column.id belongs only in proposal references. "
                    "Unknown objects and excluded columns remain forbidden."
                )
            return result

    def _table(self, table: str) -> TableSpec:
        if not isinstance(table,str):
            raise LightGraphError("unauthorized_object")
        if table in self.tables:
            return self.tables[table]
        aliases=self._table_aliases.get(table,[])
        if len(aliases)!=1:
            raise LightGraphError("unauthorized_object")
        return self.tables[aliases[0]]

    def _columns(self, table: TableSpec, columns) -> list[str]:
        if not isinstance(columns, list) or not 1 <= len(columns) <= 32 or any(not isinstance(c, str) for c in columns):
            raise LightGraphError("invalid_column_selection")
        if len(columns)!=len(set(columns)) or not set(columns)<=set(table.column_names):
            raise LightGraphError('unauthorized_column')
        return columns

    def _get_metadata(self, table=None):
        tables = [self._table(table)] if table is not None else list(self.tables.values())
        if table is not None:
            self._activate_tables(tables)
        entries = []
        for entry in tables:
            value = entry.model_dump(mode="json", by_alias=True)
            value["qualified_name"] = entry.qualified_name
            value["tool_table_id"] = f'{entry.source_id}:{entry.qualified_name}'
            source_adapter = self.source_adapters.get(entry.source_id)
            dialect = source_adapter.compiler.dialect if source_adapter else self.compiler.dialect
            value["sql_dialect"] = dialect
            value["sql_table"] = f'{ident(entry.schema_name,dialect)}.{ident(entry.name,dialect)}'
            value["sql_readable_on_current_source"] = entry.source_id in self._local_source_ids
            for column in value["columns"]:
                column["id"] = "c_" + digest(entry.source_id, entry.qualified_name, column["name"])[:16]
                column["sql_identifier"] = ident(column["name"], dialect)
            entries.append(value)
        return {"data": {"tables": entries, "identifier_usage": {
                    "table_arguments": "Use tool_table_id in metadata, profile, FD and reference tool table arguments.",
                    "column_arguments": "Use column.name in profile, FD and reference tool column arguments.",
                    "sql": "Use sql_table and column.sql_identifier for SQL; only tables marked sql_readable_on_current_source are available.",
                    "proposal_references": "Use the issued column.id in proposal column references, never a SQL identifier or table argument.",
                }}, "coverage": {"source_scope": "authorized_metadata", "result_complete": True, "truncated": False}}

    def _search_knowledge(self, table, columns=None, page=0):
        source = self._table(table)
        names = self._columns(source, columns) if columns is not None else source.column_names
        self._activate_tables([source.model_copy(update={'columns':[c for c in source.columns if c.name in names]})])
        from lightgraph.context import document_batches
        if type(page) is not int or page < 0:
            raise LightGraphError('invalid_tool_arguments')
        pages=list(document_batches(self.context_documents(),max_bytes=min(12000,self._max_bytes//3))) or [[]]
        if page >= len(pages):
            raise LightGraphError('invalid_tool_arguments')
        return {"data": {"sections": pages[page], "page":page,
                "next_page":page+1 if page+1<len(pages) else None},
                "coverage": {"source_scope": "scoped_documents", "result_complete": len(pages)==1,
                "truncated": len(pages)>1, "selection": "document_window"}}

    def _query(self, query, *, row_limit):
        # Both compiler and this executor use the narrowed catalog. The adapter
        # then revalidates again against its own connection-bound catalog.
        query = self.compiler.guard.validate(query.sql, query.parameters, operator=query.operator)
        self._activate_query(query.sql)
        self.budget.admit(Cost(db_cu=1, memory_bytes=self._max_bytes))
        if hasattr(self.adapter, "query_budget"):
            self.adapter.query_budget.deadline = min(self.adapter.query_budget.deadline, self.budget.deadline_monotonic)
        began = self.adapter.stats.queries
        try:
            yield from self.adapter.execute(query, row_limit=row_limit)
        finally:
            self.budget.charge(Cost(db_cu=max(0, self.adapter.stats.queries - began)))

    def _aggregate(self, query):
        result = None
        for batch in self._query(query, row_limit=1):
            with batch:
                for row in batch:
                    if result is not None:
                        raise LightGraphError("invalid_aggregate_shape")
                    # Gauss SUM(bigint), including SUM(COUNT(*)), may return
                    # Decimal. Source adapters intentionally preserve it as a
                    # string. These compiler-owned fields are integer counts,
                    # never arbitrary source text or general SQL projections.
                    result = {name: aggregate_count(value) for name, value in zip(batch.columns, row)}
        return result or {}

    def _profile_columns(self, table, columns):
        source = self._table(table)
        names = self._columns(source, columns)
        with self.adapter.snapshot():
            results = {name: self._aggregate(self.compiler.profile_column(source, name)) for name in names}
        return {"aggregates": results, "aggregate_columns": self._aggregate_columns(source, names),
                "coverage": self._aggregate_coverage()}

    def _check_fd(self, table, keys, values):
        source = self._table(table)
        keys = self._columns(source, keys)
        values = self._columns(source, values)
        with self.adapter.snapshot():
            nulls = self._aggregate(self.compiler.profile_column_group(source, keys)).get("null_key_rows", 0)
            results = {value: {**self._aggregate(self.compiler.check_fd(source, keys, value)), "null_key_rows": nulls} for value in values}
        return {"aggregates": results, "aggregate_columns": self._aggregate_columns(source, values),
                "coverage": self._aggregate_coverage()}

    @staticmethod
    def _aggregate_columns(source, names):
        return {name: 'c_' + digest(source.source_id, source.qualified_name, name)[:16]
                for name in names}

    def _check_reference(self, source_table, source_columns, target_table, target_columns):
        source, target = self._table(source_table), self._table(target_table)
        source_columns = self._columns(source, source_columns)
        target_columns = self._columns(target, target_columns)
        if source.source_id != target.source_id:
            from lightgraph.cross_source import check_cross_source_reference
            self._activate_tables([
                t.model_copy(update={'columns':[c for c in t.columns if c.name in names]})
                for t,names in ((source,source_columns),(target,target_columns))])
            adapters=[self.source_adapters[source.source_id],self.source_adapters[target.source_id]]
            # Each endpoint uses count, bounded DISTINCT and version recheck.
            self.budget.admit(Cost(db_cu=6,memory_bytes=self._max_bytes))
            for adapter in adapters:
                if hasattr(adapter,'query_budget'):
                    adapter.query_budget.deadline=min(adapter.query_budget.deadline,self.budget.deadline_monotonic)
            before=[a.stats.queries for a in adapters]
            try:
                result=check_cross_source_reference(adapters[0],source,source_columns,adapters[1],target,target_columns,
                    max_working_bytes=self._max_bytes,metadata_only=not self.allow_source_samples)
            finally:
                self.budget.charge(Cost(db_cu=sum(max(0,a.stats.queries-n) for a,n in zip(adapters,before))))
            counts={'source_ref_tuples','null_refs','partial_null_refs','unmatched','ambiguous','unique_matches'}
            return {'aggregates':{k:v for k,v in result.items() if k in counts},
                    'coverage':{k:v for k,v in result.items() if k not in counts|{'sample','rows'}}}
        query = self.compiler.check_reference(source, source_columns, target, target_columns)
        return {"aggregates": self._aggregate(query), "coverage": self._aggregate_coverage()}

    @staticmethod
    def _aggregate_coverage():
        return {"source_scope": "full_authorized_table_aggregate", "result_complete": True,
                "truncated": False, "source_scan": "unknown", "semantic_relationship_confirmed": False}

    def _execute_readonly_sql(self, sql, parameters=None, max_rows=None):
        if not isinstance(sql, str) or not 1 <= len(sql) <= 20000:
            raise LightGraphError("invalid_tool_arguments")
        parameters = {} if parameters is None else parameters
        if not isinstance(parameters, dict) or len(parameters) > 64:
            raise LightGraphError("invalid_tool_arguments")
        for name, value in parameters.items():
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name):
                raise LightGraphError("invalid_tool_arguments")
            if not isinstance(value, (str, int, float, bool, type(None))) or isinstance(value, float) and not math.isfinite(value):
                raise LightGraphError("invalid_tool_arguments")
        cap = self._max_rows if max_rows is None else max_rows
        if type(cap) is not int or not 1 <= cap <= self._max_rows:
            raise LightGraphError("invalid_result_limit")
        self.compiler.guard.validate(sql, parameters)
        tree = sqlglot.parse_one(sql, read=self.compiler.dialect)
        if list(tree.find_all(exp.Parameter)):
            raise LightGraphError("unsafe_sql")
        requested_limit = None
        if limit := tree.args.get("limit"):
            expression = limit.expression
            if isinstance(expression, exp.Literal) and not expression.is_string:
                requested_limit = int(expression.this)
            elif isinstance(expression, exp.Placeholder):
                requested_limit = parameters.get(expression.name)
            if type(requested_limit) is not int or requested_limit < 0:
                raise LightGraphError("invalid_result_limit")
        effective_limit = min(cap + 1, requested_limit) if requested_limit is not None else cap + 1
        # Only the result page limit is narrowed. Never remove clauses, alter
        # predicates or pretend a capped page validates a full source table.
        bounded = render_select(tree.limit(effective_limit, copy=True), self.compiler.dialect)
        query = self.compiler.finish(bounded, parameters, "agent_select")
        rows, columns, byte_count, truncated = [], [], 0, False
        stream = self._query(query, row_limit=cap + 1)
        try:
            for batch in stream:
                with batch:
                    columns = list(batch.columns)
                    for row in batch:
                        size = len(canonical(list(row)).encode())
                        if len(rows) >= cap or byte_count + size > self._max_bytes // 2:
                            truncated = True
                            break
                        rows.append(list(row))
                        byte_count += size
                    if truncated:
                        break
        finally:
            stream.close()
        return {"data": {"columns": columns, "rows": rows},
                "coverage": {"source_scope": "query_result", "result_complete": not truncated,
                             "truncated": truncated, "source_scan": "unknown", "requested_limit": requested_limit,
                             "returned_rows": len(rows), "full_source_validation": False}}

    def close(self):
        """Release references owned here; the caller owns adapter lifecycle."""
        self._closed = True
        self.tables.clear()
        self.compiler.tables.clear()
        self.compiler.guard.tables.clear()
        self.compiler.guard.physical_columns.clear()
        self._table_aliases.clear()
        self._reserved_probe_ids.clear()
        self.source_adapters.clear()
