from __future__ import annotations
import os
import re
from pathlib import Path
from typing import Literal
import yaml
from pydantic import ConfigDict, Field, PrivateAttr, SecretStr, StrictBool, StrictInt, field_validator, model_serializer, model_validator
from ..contracts import StrictModel, QueryLimits, ExtractionGoal
from ..errors import LightGraphError

class SourceConfig(StrictModel):
    id: str
    kind: Literal["excel_workbook", "csv_file", "gauss_database", "mysql_database"]
    path: str | None = None
    dsn_env: str | None = None
    credential_mode: Literal["environment", "session"] = "environment"
    # Opaque non-secret revision invalidates cached metadata when a direct
    # connection changes. The DSN itself belongs only to the web session.
    credential_revision: str | None = None
    allowed_tables: list[str] = Field(default_factory=list)
    allowed_columns: dict[str,list[str]] | None = None
    capability_manifest: str | None = None
    # Explicit UI permission is per source. None retains legacy manifest-based
    # configuration; False permits catalog reads only, even with a manifest.
    allow_fact_data: StrictBool | None = None
    include_hidden: bool = False
    header_row: int = Field(default=1, ge=1)
    encoding: str | None = None
    delimiter: str | None = None
    header: bool | None = None
    null_values: list[str] | None = None
    type_inference: Literal["text"] | None = None
    columns: list[str] | None = None
    table_name: str | None = None
    schema_name: str = "public"

    @model_validator(mode="after")
    def check_format(self):
        if not self.id or not self.id.strip():
            raise ValueError("source_id_required")
        if self.kind == "csv_file":
            if (not self.path or not self.encoding or not self.delimiter or
                    self.header is None or self.null_values is None or
                    self.type_inference is None or
                    len(self.delimiter) != 1 or self.delimiter in '\r\n"\x00' or
                    (self.header and self.columns is not None) or
                    (not self.header and (not self.columns or self.header_row != 1))):
                raise ValueError("csv_format_config_required")
        return self

class ModelConfig(StrictModel):
    # Validation errors must never echo a configuration containing credentials.
    model_config = ConfigDict(hide_input_in_errors=True)
    mode: Literal["disabled", "metadata_only", "in_environment", "approved_external"] = "disabled"
    base_url: str = ""
    model: str = ""
    response_format: Literal["json_schema", "json_object"] = "json_schema"
    api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    api_key_env: str = "LIGHTGRAPH_API_KEY"
    stream: bool = False
    stream_options_include_usage: bool = True
    allow_insecure_http: bool = False
    # Explicit null disables only this API total; counters and other limits remain.
    max_calls: int | None = Field(default=2000, ge=0)
    max_tokens: int | None = Field(default=None, ge=0,
        description="Legacy cumulative-token setting retained for configuration migration; Harness budgets physical calls and wall time.")
    max_discovery_steps: int = Field(default=16, ge=1, le=32,
        description="Legacy per-table setting, accepted when reading old configurations; not used by Harness.")
    max_tool_calls: int = Field(default=16, ge=1, le=64,
        description="Legacy source-tool allowance; not used by Harness.")
    timeout_seconds: float = Field(default=800, gt=0)
    max_output_tokens: int = Field(default=65536, gt=0)
    # Read legacy byte settings without presenting them as Harness controls.
    max_prompt_bytes: int | None = Field(default=None, gt=0,
        description="Legacy request-byte guard retained for configuration migration; not applied by Harness.")
    max_prompt_tokens: int | None = Field(default=1048576, gt=0,
        description="Optional estimated input token cap, including tool schemas; independent of byte limits.")
    context_window_tokens: int | None = Field(default=None, gt=0,
        description="Optional combined input/output token window; max_output_tokens is reserved from it.")
    working_context_tokens: int | None = Field(default=32768, gt=0,
        description="Harness working-context target; null disables automatic compaction including overflow recovery. Token-meter estimates are calibrated from provider usage.")
    max_response_bytes: int | None = Field(default=None, gt=0,
        description="Legacy response-byte guard retained for configuration migration; not applied by Harness.")
    retries: int = Field(default=1, ge=0, le=1)

    @field_validator("base_url")
    @classmethod
    def normalize_endpoint(cls, value):
        from .model_endpoint import normalize_model_base_url
        return normalize_model_base_url(value)

    @field_validator("model", "api_key_env")
    @classmethod
    def normalize_model_identifier(cls, value, info):
        value = value.strip()
        if info.field_name == "api_key_env" and not value:
            value = "LIGHTGRAPH_API_KEY"
        if info.field_name == "api_key_env" and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError("invalid_model_key_environment")
        return value

    @model_validator(mode="after")
    def check_context_window(self):
        if self.context_window_tokens is not None and self.context_window_tokens <= self.max_output_tokens:
            raise ValueError("context_window_must_exceed_output_reservation")
        return self


class AgentConfig(StrictModel):
    """Harness configuration; old framework names migrate, never select an old engine."""
    model_config = ConfigDict(hide_input_in_errors=True)
    framework: Literal["deepseek_harness"] = "deepseek_harness"
    name: str = Field(default="lightgraph-discovery", min_length=1, max_length=64)
    llm: ModelConfig

    @model_validator(mode="before")
    @classmethod
    def migrate_framework(cls, value):
        if isinstance(value, dict) and value.get("framework") == "agentscope":
            return {**value, "framework": "deepseek_harness"}
        return value


class GraphScopeBinding(StrictModel):
    """Operator-owned visibility mapping; never accepted from an Agent draft."""
    source_id: str = Field(min_length=1)
    schema_name: str = Field(default='public', alias='schema')
    table: str = Field(min_length=1)
    columns: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode='after')
    def check_columns(self):
        if any(not column for column in self.columns) or len(set(self.columns)) != len(self.columns):
            raise ValueError('invalid_scope_binding_columns')
        return self


class RunConfig(StrictModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    _agent: AgentConfig | None = PrivateAttr(default=None)
    agent_config: str | None = Field(default=None, min_length=1)
    isolation_domain: str = "default"
    sources: list[SourceConfig]
    documents: list[str] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)
    goal: ExtractionGoal = Field(default_factory=ExtractionGoal)
    goal_markdown: str | None = Field(default=None, max_length=64000,
        description="User-authored Markdown construction objective. Null renders the legacy goal fields; source authorization remains in typed configuration.")
    limits: QueryLimits = Field(default_factory=QueryLimits)
    model: ModelConfig = Field(default_factory=ModelConfig)
    output_dir: str = "runs/default"
    record_trace: bool = Field(default=True,
        description="Record the complete local run trajectory with credentials redacted; retain until manually cleared.")
    scopes: list[str] = Field(default_factory=lambda:["public"])
    scope_bindings: list[GraphScopeBinding] = Field(default_factory=list, max_length=1024,
        description='Operator-owned source/table columns determining each result support scope. No binding means public; never inferred from identity keys.')
    max_working_bytes: int = Field(default=32*1024*1024, gt=0)
    max_db_queries: StrictInt | None = Field(default=None, gt=0,
        description="Total source-query admissions, including metadata and transaction controls. Null removes only the query-count ceiling.")
    construction_target: Literal["instances", "structure"] = Field(default="instances",
        description="Build concrete instance nodes and edges, or publish only structure and source mappings with no claim of instance validation.")
    max_partitions: int = Field(default=256, ge=1, le=4096)
    max_discovery_rounds: int = Field(default=3, ge=1, le=8)
    # Accepted only to read old project configurations. Harness does not split
    # discovery into fixed object/relation windows.
    discovery_time_fraction: float = Field(default=0.75, gt=0, lt=1)
    discovery_query_fraction: float = Field(default=0.75, gt=0, lt=1)
    max_relation_discovery_attempts: int = Field(default=2, ge=1, le=8)
    max_reconciliation_entities: int = Field(default=5000, ge=1, le=100000)
    max_reconciliation_candidates: int = Field(default=256, ge=1, le=4096)
    max_identity_reviews: int = Field(default=8, ge=0, le=64)
    max_run_seconds: float = Field(default=60000, gt=0)
    # Customer deployment must attest all external retention paths before live instance access.
    deployment_retention_verified: bool = False
    rediscover: bool = False
    document_scopes: list[str] = Field(default_factory=lambda:["public"])
    removed_document_ids: list[str] = Field(default_factory=list)
    removed_rule_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def check_model_configuration_source(cls, value):
        # Never merge two credential-bearing sources, even when values agree.
        if isinstance(value, dict) and value.get("agent_config"):
            inline = value.get("model")
            configured = bool(inline.model_fields_set) if isinstance(inline, ModelConfig) else bool(inline)
            if configured:
                raise ValueError("agent_config_and_model_are_mutually_exclusive")
        return value

    @model_serializer(mode="wrap")
    def serialize_config(self, handler):
        data = handler(self)
        if self.agent_config:
            data.pop("model", None)
        return data

    @property
    def resolved_model(self) -> ModelConfig:
        if self.agent_config:
            if self._agent is None:
                raise LightGraphError("agent_config_unresolved")
            return self._agent.llm
        return self.model

    @property
    def agent_name(self) -> str:
        return self._agent.name if self._agent is not None else "lightgraph-discovery"

    @model_validator(mode="after")
    def check_source_ids(self):
        if len({s.id for s in self.sources})!=len(self.sources):
            raise ValueError("source_ids_must_be_unique")
        if not self.document_scopes:raise ValueError("document_scope_required")
        return self

    def resolve_paths(self, base:Path):
        if self.agent_config:
            agent_path = (base / self.agent_config).resolve()
            self._agent = load_agent_config(agent_path)
            # Do not copy resolved LLM settings into model: a serialized run must
            # still refer to one configuration source, not a conflicting pair.
            object.__setattr__(self, "agent_config", str(agent_path))
        for src in self.sources:
            if src.path:src.path=str((base/src.path).resolve())
            if src.capability_manifest:src.capability_manifest=str((base/src.capability_manifest).resolve())
        self.documents=[str((base/p).resolve()) for p in self.documents]
        self.rules=[str((base/p).resolve()) for p in self.rules]
        self.output_dir=str((base/self.output_dir).resolve())
        return self


def load_agent_config(path) -> AgentConfig:
    try:
        return AgentConfig.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
    except Exception:
        raise LightGraphError("agent_config_invalid") from None


def load_config(path):
    try:
        path=Path(path).resolve()
        return RunConfig.model_validate(yaml.safe_load(path.read_text())).resolve_paths(path.parent)
    except Exception:
        raise LightGraphError("config_invalid") from None


def open_sources(config, *, metadata_only=False, source_credentials=None, isolate_database_failures=False):
    from ..sources import ExcelSource, CSVSource, GaussSource, MySQLSource
    sources=[]
    from ..sources.budget import QueryBudget
    budget=QueryBudget(max_queries=config.max_db_queries,max_run_seconds=config.max_run_seconds)
    try:
        for s in config.sources:
            if s.kind=="excel_workbook":
                if not s.path:raise LightGraphError("config_invalid")
                sources.append(ExcelSource(s.path,source_id=s.id,limits=config.limits,
                    header_row=s.header_row,include_hidden=s.include_hidden,query_budget=budget,
                    max_memory_bytes=config.max_working_bytes,metadata_only=metadata_only))
            elif s.kind=="csv_file":
                sources.append(CSVSource(s.path,source_id=s.id,limits=config.limits,
                    encoding=s.encoding,delimiter=s.delimiter,header=s.header,
                    null_values=s.null_values,type_inference=s.type_inference,
                    columns=s.columns,table_name=s.table_name,schema_name=s.schema_name,
                    header_row=s.header_row,max_memory_bytes=config.max_working_bytes,
                    query_budget=budget,metadata_only=metadata_only))
            else:
                source_metadata_only = metadata_only or s.allow_fact_data is False
                auto_verify = s.allow_fact_data is True and not source_metadata_only
                if not source_metadata_only and not auto_verify and not config.deployment_retention_verified:
                    raise LightGraphError("deployment_retention_unverified")
                dsn=(source_credentials or {}).get(s.id)
                if not dsn and s.credential_mode == "environment":
                    dsn=os.getenv(s.dsn_env or "")
                if not dsn:raise LightGraphError("source_credential_missing")
                if not s.allowed_tables:raise LightGraphError("source_scope_missing")
                verified=None
                if s.capability_manifest and not source_metadata_only and not auto_verify:
                    from ..contracts import CapabilityManifest
                    try:
                        path=Path(s.capability_manifest)
                        if path.stat().st_size > 2*1024*1024:
                            raise ValueError('capability_manifest_too_large')
                        text=path.read_text(encoding='utf-8')
                        if any(isinstance(t,(yaml.tokens.AliasToken,yaml.tokens.AnchorToken,yaml.tokens.TagToken))
                               for t in yaml.scan(text)):
                            raise ValueError('capability_manifest_alias_forbidden')
                        records=yaml.safe_load(text)
                        verified=CapabilityManifest.model_validate(records[s.id])
                    except Exception:raise LightGraphError("capability_manifest_invalid") from None
                adapter=MySQLSource if s.kind=='mysql_database' else GaussSource
                options = {}
                if source_metadata_only:
                    options['metadata_only'] = True
                if auto_verify:
                    # Verify the current connection, rather than treating the
                    # checkbox or a previously uploaded file as capability proof.
                    options['probe_runtime_limits'] = True
                try:
                    sources.append(adapter(dsn,source_id=s.id,allowed_tables=s.allowed_tables,
                        allowed_columns=s.allowed_columns,limits=config.limits,verified_capabilities=verified,query_budget=budget,**options))
                except LightGraphError as exc:
                    from ..sources.unavailable import UnavailableSource, isolatable_initialization_failure
                    if not isolate_database_failures or not isolatable_initialization_failure(exc):
                        raise
                    # Do not turn an exhausted shared run into a per-source
                    # outage or grant a fresh budget to the remaining sources.
                    budget.check_time()
                    if budget.snapshot()['remaining_queries'] == 0:
                        from ..errors import BudgetExceeded
                        raise BudgetExceeded('query_budget_exhausted') from None
                    sources.append(UnavailableSource(s, exc, limits=config.limits, query_budget=budget))
        return sources
    except Exception:
        for s in sources:s.close()
        raise
