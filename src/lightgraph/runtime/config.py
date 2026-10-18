from __future__ import annotations
import os
from pathlib import Path
from typing import Literal
import yaml
from pydantic import Field, model_validator
from ..contracts import StrictModel, QueryLimits, ExtractionGoal
from ..errors import LightGraphError

class SourceConfig(StrictModel):
    id: str
    kind: Literal["excel_workbook", "gauss_database"]
    path: str | None = None
    dsn_env: str | None = None
    allowed_tables: list[str] = Field(default_factory=list)
    allowed_columns: dict[str,list[str]] | None = None
    capability_manifest: str | None = None
    include_hidden: bool = False
    header_row: int = Field(default=1, ge=1)

class ModelConfig(StrictModel):
    mode: Literal["disabled", "metadata_only", "in_environment", "approved_external"] = "disabled"
    base_url: str = ""
    model: str = ""
    api_key_env: str = "LIGHTGRAPH_API_KEY"
    max_calls: int = Field(default=40, ge=0)
    max_tokens: int = Field(default=120000, ge=0)
    timeout_seconds: float = Field(default=60, gt=0)

class RunConfig(StrictModel):
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

    @model_validator(mode="after")
    def check_source_ids(self):
        if len({s.id for s in self.sources})!=len(self.sources):
            raise ValueError("source_ids_must_be_unique")
        if not self.document_scopes:raise ValueError("document_scope_required")
        return self

    def resolve_paths(self, base:Path):
        for src in self.sources:
            if src.path:src.path=str((base/src.path).resolve())
            if src.capability_manifest:src.capability_manifest=str((base/src.capability_manifest).resolve())
        self.documents=[str((base/p).resolve()) for p in self.documents]
        self.rules=[str((base/p).resolve()) for p in self.rules]
        self.output_dir=str((base/self.output_dir).resolve())
        return self


def load_config(path):
    try:
        path=Path(path).resolve()
        return RunConfig.model_validate(yaml.safe_load(path.read_text())).resolve_paths(path.parent)
    except Exception:
        raise LightGraphError("config_invalid") from None


def open_sources(config, *, metadata_only=False):
    from ..sources import ExcelSource, GaussSource
    sources=[]
    from ..sources.budget import QueryBudget
    budget=QueryBudget(max_queries=config.max_db_queries,max_run_seconds=config.max_run_seconds)
    try:
        for s in config.sources:
            if s.kind=="excel_workbook":
                if not s.path:raise LightGraphError("config_invalid")
                sources.append(ExcelSource(s.path,source_id=s.id,limits=config.limits,
                    header_row=s.header_row,include_hidden=s.include_hidden,query_budget=budget))
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
