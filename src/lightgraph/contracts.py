"""Shared typed contracts. Source observations are ephemeral; only declared outputs persist."""
from __future__ import annotations
import hashlib
import json
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

Scalar = str | int | float | bool | None
Status = Literal["accepted", "candidate", "unresolved", "deprecated"]

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, validate_assignment=True)

def canonical(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

def digest(*values: Any) -> str:
    return hashlib.sha256(canonical(list(values)).encode("utf-8")).hexdigest()

class ColumnSpec(StrictModel):
    name: str
    data_type: str = "text"
    comment: str = ""
    nullable: bool = True
    primary_key: bool = False

class ForeignKeySpec(StrictModel):
    name: str
    columns: list[str] = Field(min_length=1)
    target_schema: str
    target_table: str
    target_columns: list[str] = Field(min_length=1)

class TableSpec(StrictModel):
    source_id: str
    schema_name: str = Field(default="public", alias="schema")
    name: str
    columns: list[ColumnSpec]
    comment: str = ""
    foreign_keys: list[ForeignKeySpec] = Field(default_factory=list)
    unique_keys: list[list[str]] = Field(default_factory=list)
    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]
    @property
    def qualified_name(self) -> str:
        return f"{self.schema_name}.{self.name}"

class SourceRef(StrictModel):
    source_id: str
    schema_name: str = Field(default="public", alias="schema")
    table: str

class CapabilityManifest(StrictModel):
    product: str = "unknown"
    version: str = "unknown"
    driver: str = "unknown"
    capabilities: dict[str, Literal["verified", "unsupported", "unknown"]] = Field(default_factory=dict)
    consistent_snapshot: bool = False
    actual_database_verified: bool = False

class QueryLimits(StrictModel):
    max_batch_rows: int = Field(default=5000, gt=0)
    max_batch_bytes: int = Field(default=65536, gt=0)
    max_result_rows: int = Field(default=20000, gt=0)
    max_result_bytes: int = Field(default=16777216, gt=0)
    timeout_ms: int = Field(default=60000, gt=0)

class ExtractionGoal(StrictModel):
    goal_id: str = "analysis_knowledge"
    entity_kinds: list[str] = Field(default_factory=lambda: ["Metric", "Measure", "Dimension", "DimensionMember", "ReportItem", "BusinessConcept"])
    relation_kinds: list[str] = Field(default_factory=lambda: ["parent_of", "member_of", "uses_measure", "verified_reference"])
    source_scope: list[str] = Field(default_factory=list)
    excluded_columns: list[str] = Field(default_factory=list)
    observation_values: bool = False

class ScopeSpec(StrictModel):
    namespaces: list[str] = Field(default_factory=list)
    predicate: dict[str, Any] | None = None
    partition_by: list[str] = Field(default_factory=list)

class IdentitySpec(StrictModel):
    key_columns: list[str] = Field(min_length=1)
    entity_kind: str = "ReportItem"
    semantic_resolution: Literal["source_scoped", "verified_definition_registry"] = "source_scoped"
    semantic_key_columns: list[str] = Field(default_factory=list)
    semantic_namespace: str = ""
    level_policy: Literal["not_applicable", "code_with_fill_merge", "level_qualified"] = "not_applicable"

class ExcludedColumn(StrictModel):
    column: str
    reason: str

class AttributeSpec(StrictModel):
    single_valued: dict[str, str] = Field(default_factory=dict)
    multi_valued: dict[str, str] = Field(default_factory=dict)
    excluded: list[ExcludedColumn] = Field(default_factory=list)

class LevelSpec(StrictModel):
    level: int = Field(gt=0)
    code: str
    label: str | None = None
    aliases: list[str] = Field(default_factory=list)

class HierarchySpec(StrictModel):
    levels: list[LevelSpec] = Field(default_factory=list)
    parent_column: str | None = None
    taxonomy: str = ""
    allow_dag: bool = False
    fill_semantics_confirmed: bool = False
    order_evidence: list[str] = Field(default_factory=list)
    gap_policy: Literal["split"] = "split"

class RelationSpec(StrictModel):
    source_rule_id: str
    target_rule_id: str
    source_columns: list[str] = Field(min_length=1)
    target_columns: list[str] = Field(min_length=1)
    relation: str = "verified_reference"
    qualifiers: dict[str, Scalar] = Field(default_factory=dict)
    source_json_array_columns: list[str] = Field(default_factory=list, max_length=1)
    target_json_array_columns: list[str] = Field(default_factory=list, max_length=1)
    max_array_elements: int = Field(default=256, gt=0, le=4096)
    @model_validator(mode='after')
    def validate_arrays(self):
        if not set(self.source_json_array_columns) <= set(self.source_columns) or not set(self.target_json_array_columns) <= set(self.target_columns):
            raise ValueError('array_column_not_in_reference_endpoint')
        if self.source_json_array_columns and self.target_json_array_columns:
            raise ValueError('two_sided_array_cartesian_forbidden')
        return self

class Dependencies(StrictModel):
    schema_ref: str = Field(default="", alias="schema")
    documents: list[str] = Field(default_factory=list)
    identity_policy: str = "identity_v1"
    operator_version: str = "1"

class Coverage(StrictModel):
    scope: Literal["full", "partition", "sampled"] = "full"
    partitions: list[str] = Field(default_factory=list)
    rows_scanned: int | None = None
    entities: int | None = None
    consistent: bool = False

class RuleEvidence(StrictModel):
    hypothesis: str | None = None
    document: list[str] = Field(default_factory=list)
    probe_refs: list[str] = Field(default_factory=list)
    semantic_basis: list[str] = Field(default_factory=list)

class RuleBudget(StrictModel):
    max_db_cu: float = Field(default=40, gt=0)
    max_rows_returned: int = Field(default=20000, gt=0)
    max_seconds: float = Field(default=120, gt=0)

class OperatorSpec(StrictModel):
    op: str
    args: dict[str, Any] = Field(default_factory=dict)
    output: str = ""

class RuleSpec(StrictModel):
    rule_id: str
    revision: int = Field(default=1, ge=1)
    kind: Literal["column_group_entity", "wide_hierarchy", "long_hierarchy", "reference"]
    goal_refs: list[str] = Field(default_factory=list)
    source: SourceRef
    scope: ScopeSpec = Field(default_factory=ScopeSpec)
    identity: IdentitySpec
    attributes: AttributeSpec = Field(default_factory=AttributeSpec)
    hierarchy: HierarchySpec | None = None
    relation: RelationSpec | None = None
    normalization: dict[str, Scalar] = Field(default_factory=lambda: {"version": "n1", "keep_leading_zeros": True})
    operators: list[OperatorSpec] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=lambda: ["metadata_reading", "read_only_transaction", "parameter_binding", "distinct_projection"])
    dependencies: Dependencies = Field(default_factory=Dependencies)
    status: Literal["draft", "validated", "canary_checked", "published", "deprecated", "rolled_back"] = "draft"
    validation_ref: str | None = None
    coverage: Coverage = Field(default_factory=Coverage)
    evidence: RuleEvidence = Field(default_factory=RuleEvidence)
    budget: RuleBudget = Field(default_factory=RuleBudget)
    publish: dict[str, Scalar] = Field(default_factory=lambda: {"mode": "staged", "target": "graph", "idempotent": True})
    stale_when: dict[str, bool] = Field(default_factory=lambda: {"schema_hash_changed": True, "doc_revision_changed": True})
    errors: dict[str, str] = Field(default_factory=dict)
    @model_validator(mode="after")
    def validate_shape(self):
        if self.publish.get("mode") != "staged":
            raise ValueError("publication_must_be_staged")
        if self.kind in ("wide_hierarchy", "long_hierarchy") and not self.hierarchy:
            raise ValueError("hierarchy_required")
        if self.kind == "reference" and not self.relation:
            raise ValueError("relation_required")
        if self.normalization != {"version": "n1", "keep_leading_zeros": True}:
            raise ValueError("unsupported_normalization")
        return self

class GraphFact(StrictModel):
    isolation_domain: str = "default"
    fact_kind: Literal["NodeExistence", "PropertyValue", "Edge", "DocumentClaim"]
    payload: dict[str, Any]
    @model_validator(mode="after")
    def normalize_payload(self):
        if self.fact_kind in ("PropertyValue", "Edge"):
            self.payload.setdefault("qualifiers", {})
        if self.fact_kind == "DocumentClaim":
            # Claim identity belongs to its meaning; document revision/section
            # belongs exclusively to the supporting publication.
            fields = ("subject", "predicate", "object", "modality", "conditions")
            if all(field in self.payload for field in fields):
                conditions = self.payload["conditions"]
                if isinstance(conditions, dict) and all(str(key).isdigit() for key in conditions) and all(isinstance(value, str) for value in conditions.values()):
                    self.payload["conditions"] = {str(i): value for i, value in enumerate(sorted(set(conditions.values())))}
                self.payload["id"] = "claim_" + digest(self.isolation_domain, {field: self.payload[field] for field in fields})
        return self
    @property
    def fact_id(self) -> str:
        return digest(self.isolation_domain, self.fact_kind, self.payload)

class SupportSpec(StrictModel):
    origin: Literal["catalog", "database_rule", "document_claim", "user_input"] = "database_rule"
    source_binding: str
    scope_ref: str
    partition_id: str
    rule_id: str
    rule_revision: int
    validation_ref: str | None = None
    document_ref: str | None = None
    binding_revision: int = 1
    status: Status = "accepted"
    def support_id(self, fact_id: str) -> str:
        return digest(self.origin, self.source_binding, self.rule_id, self.rule_revision, self.scope_ref, self.partition_id, fact_id)

class PartitionCompletion(StrictModel):
    publication_id: str
    complete: bool
    consistent: bool
    dependencies_valid: bool
    truncated: bool = False
    checkpoint_ref: str | None = None

class ValidationRecord(StrictModel):
    validation_id: str
    rule_id: str
    checks: dict[str, int | float | bool] = Field(default_factory=dict)
    coverage: Coverage = Field(default_factory=Coverage)
    status: Literal["validated", "unresolved", "rejected", "deferred"]
    codes: list[str] = Field(default_factory=list)

class RunReport(StrictModel):
    run_id: str
    status: Literal["COMPLETED", "COMPLETED_WITH_GAPS", "PARTIAL", "FAILED"] = "COMPLETED"
    rules_published: list[str] = Field(default_factory=list)
    rules_skipped: list[str] = Field(default_factory=list)
    issues: list[dict[str, str | int]] = Field(default_factory=list)
    counts: dict[str, int | float] = Field(default_factory=dict)
    validation: dict[str, str] = Field(default_factory=lambda: {"E": "not_run", "D": "not_run", "I": "not_run"})
