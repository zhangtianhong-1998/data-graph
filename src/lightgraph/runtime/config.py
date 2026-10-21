from __future__ import annotations
import os
from pathlib import Path
from typing import Literal
import yaml
from pydantic import ConfigDict, Field, PrivateAttr, SecretStr, model_serializer, model_validator
from ..contracts import StrictModel, QueryLimits, ExtractionGoal
from ..errors import LightGraphError

class SourceConfig(StrictModel):
    id: str
    kind: Literal["excel_workbook", "csv_file", "gauss_database"]
    path: str | None = None
    dsn_env: str | None = None
    allowed_tables: list[str] = Field(default_factory=list)
    allowed_columns: dict[str,list[str]] | None = None
    capability_manifest: str | None = None
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
    api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    api_key_env: str = "LIGHTGRAPH_API_KEY"
    stream: bool = False
    stream_options_include_usage: bool = True
    allow_insecure_http: bool = False
    max_calls: int = Field(default=40, ge=0)
    max_tokens: int = Field(default=120000, ge=0)
    max_discovery_steps: int = Field(default=8, ge=1, le=32)
    max_tool_calls: int = Field(default=16, ge=1, le=64)
    timeout_seconds: float = Field(default=60, gt=0)
    max_output_tokens: int = Field(default=4096, gt=0)
    max_prompt_bytes: int = Field(default=65536, gt=0)
    max_response_bytes: int = Field(default=262144, gt=0)
    retries: int = Field(default=1, ge=0, le=1)


class AgentConfig(StrictModel):
    """Separate reusable AgentScope settings; secrets never enter dumps or repr."""
    model_config = ConfigDict(hide_input_in_errors=True)
    framework: Literal["agentscope"] = "agentscope"
    name: str = Field(default="lightgraph-discovery", min_length=1, max_length=64)
    llm: ModelConfig


class RunConfig(StrictModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    _agent: AgentConfig | None = PrivateAttr(default=None)
    agent_config: str | None = Field(default=None, min_length=1)
    isolation_domain: str = "default"
    sources: list[SourceConfig]
    documents: list[str] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)
    goal: ExtractionGoal = Field(default_factory=ExtractionGoal)
    limits: QueryLimits = Field(default_factory=QueryLimits)
    model: ModelConfig = Field(default_factory=ModelConfig)
    output_dir: str = "runs/default"
    scopes: list[str] = Field(default_factory=lambda:["public"])
    max_working_bytes: int = Field(default=32*1024*1024, gt=0)
    max_db_queries: int = Field(default=240, gt=0)
    max_partitions: int = Field(default=256, ge=1, le=4096)
    max_run_seconds: float = Field(default=1800, gt=0)
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


def open_sources(config, *, metadata_only=False):
    from ..sources import ExcelSource, CSVSource, GaussSource
    sources=[]
    from ..sources.budget import QueryBudget
    budget=QueryBudget(max_queries=config.max_db_queries,max_run_seconds=config.max_run_seconds)
    try:
        for s in config.sources:
            if s.kind=="excel_workbook":
                if not s.path:raise LightGraphError("config_invalid")
                sources.append(ExcelSource(s.path,source_id=s.id,limits=config.limits,
                    header_row=s.header_row,include_hidden=s.include_hidden,query_budget=budget))
            elif s.kind=="csv_file":
                sources.append(CSVSource(s.path,source_id=s.id,limits=config.limits,
                    encoding=s.encoding,delimiter=s.delimiter,header=s.header,
                    null_values=s.null_values,type_inference=s.type_inference,
                    columns=s.columns,table_name=s.table_name,schema_name=s.schema_name,
                    header_row=s.header_row,max_memory_bytes=config.max_working_bytes,
                    query_budget=budget,metadata_only=metadata_only))
            else:
                if not metadata_only and not config.deployment_retention_verified:
                    raise LightGraphError("deployment_retention_unverified")
                dsn=os.getenv(s.dsn_env or "")
                if not dsn:raise LightGraphError("source_credential_missing")
                if not s.allowed_tables:raise LightGraphError("source_scope_missing")
                verified=None
                if s.capability_manifest and not metadata_only:
                    import json
                    from ..contracts import CapabilityManifest
                    try:
                        records=json.loads(Path(s.capability_manifest).read_text())
                        verified=CapabilityManifest.model_validate(records[s.id])
                    except Exception:raise LightGraphError("capability_manifest_invalid") from None
                sources.append(GaussSource(dsn,source_id=s.id,allowed_tables=s.allowed_tables,
                    allowed_columns=s.allowed_columns,limits=config.limits,verified_capabilities=verified,query_budget=budget))
        return sources
    except Exception:
        for s in sources:s.close()
        raise
