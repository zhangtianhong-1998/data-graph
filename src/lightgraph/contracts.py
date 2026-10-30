"""Shared typed contracts. Source observations are ephemeral; only declared outputs persist."""
from __future__ import annotations
import hashlib
import json
from typing import Annotated, Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .semantic_schema import SemanticSchema

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

class RuntimeGuidance(StrictModel):
    """Explicit operator instructions, selected for each ephemeral Agent call."""
    text: str = Field(min_length=1, max_length=4000)
    injection: Literal['global', 'task', 'iteration', 'semantic_review'] = 'global'
    stages: list[Literal['T0_discovery', 'T1_table_discovery', 'T2_cross_table',
        'T3_hierarchy', 'T4_document', 'T5_revision', 'semantic_review']] = Field(default_factory=list, max_length=8)

    @model_validator(mode='after')
    def usable_stage(self):
        if not self.text.strip():
            raise ValueError('empty_runtime_guidance')
        discovery={'T0_discovery','T1_table_discovery','T2_cross_table','T3_hierarchy','T5_revision'}
        allowed={'global':discovery|{'T4_document','semantic_review'},
                 'task':discovery|{'T4_document'},'iteration':discovery,
                 'semantic_review':{'semantic_review'}}[self.injection]
        if set(self.stages)-allowed:
            raise ValueError('runtime_guidance_stage_mismatch')
        return self


class ExtractionGoal(StrictModel):
    """Operator intent and output scope; these never grant source permissions."""
    goal_id: str = "analysis_knowledge"
    purpose: str = Field(default="构建业务分析所需的实体和关系，避免展开逐笔业务观测。", max_length=4000)
    include: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(default_factory=list, max_length=16)
    exclude: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(default_factory=list, max_length=16)
    discovery_guidance: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(default_factory=list, max_length=16)
    runtime_guidance: list[RuntimeGuidance] = Field(default_factory=list, max_length=16)
    entity_kinds: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(default_factory=list, max_length=64)
    relation_kinds: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(default_factory=list, max_length=128)
    source_scope: list[str] = Field(default_factory=list)
    excluded_columns: list[str] = Field(default_factory=list)
    # Unlike excluded_columns, these can be read to test a hypothesis but their
    # values must not become entity identities, properties or relationship ends.
    evidence_only_columns: list[str] = Field(default_factory=list)
    observation_values: bool = False
    # Empty kind lists let the Agent propose a bounded, explicitly defined
    # vocabulary. They do not expand table/column or value-output permissions.
    semantic_schema: SemanticSchema = Field(default_factory=SemanticSchema)

class ScopeSpec(StrictModel):
    namespaces: list[str] = Field(default_factory=list)
    predicate: dict[str, Any] | None = None
    partition_by: list[str] = Field(default_factory=list)

class IdentitySpec(StrictModel):
    key_columns: list[str] = Field(min_length=1)
    entity_kind: str = "ReportItem"
    semantic_resolution: Literal["source_scoped", "verified_definition_registry", "verified_identity_domain"] = "source_scoped"
    semantic_key_columns: list[str] = Field(default_factory=list)
    semantic_namespace: str = ""
    level_policy: Literal["not_applicable", "code_with_fill_merge", "level_qualified", "relative_path"] = "not_applicable"
    identity_domain: str = ""
    identity_evidence: list[str] = Field(default_factory=list)
    attribute_evidence: list[str] = Field(default_factory=list)
    identity_scope: Literal["namespace_local", "domain_global"] = "namespace_local"
    global_scope_evidence: list[str] = Field(default_factory=list)
    @model_validator(mode="after")
    def validate_identity_domain(self):
        if any(not ref.strip() for refs in (self.identity_evidence, self.attribute_evidence, self.global_scope_evidence)
               for ref in refs):
            raise ValueError("empty_identity_evidence_reference")
        if self.semantic_resolution == "verified_identity_domain":
            if not self.identity_domain.strip() or not self.identity_evidence:
                raise ValueError("verified_identity_domain_evidence_required")
            if self.semantic_key_columns or self.semantic_namespace:
                raise ValueError("identity_domain_definition_policy_conflict")
            if self.level_policy in {"level_qualified", "code_with_fill_merge"}:
                raise ValueError("identity_domain_level_policy_conflict")
            if self.identity_scope == "domain_global" and not self.global_scope_evidence:
                raise ValueError("global_identity_scope_evidence_required")
        elif self.level_policy == "relative_path":
            raise ValueError("relative_path_requires_verified_identity_domain")
        return self

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
    # Ordinal slots 1 and 3 imply a missing slot; ordered slots merely sort the
    # available columns. Relative-path rules must explicitly choose one.
    position_semantics: Literal["ordinal_slots", "ordered_slots"] | None = None
    position_evidence: list[str] = Field(default_factory=list)
    context_qualifiers: dict[str, Scalar] = Field(default_factory=dict)
    semantic_levels: dict[int, str] = Field(default_factory=dict)
    @model_validator(mode="after")
    def validate_hierarchy_context(self):
        if len({level.level for level in self.levels}) != len(self.levels):
            raise ValueError("duplicate_hierarchy_position")
        if set(self.context_qualifiers) - {"condition", "valid_period", "period", "hierarchy_version"}:
            raise ValueError("invalid_hierarchy_context")
        if any(value is None or value == "" for value in self.context_qualifiers.values()):
            raise ValueError("empty_hierarchy_context")
        if any(not ref.strip() for ref in self.position_evidence):
            raise ValueError("empty_position_evidence_reference")
        if set(self.semantic_levels) - {level.level for level in self.levels}:
            raise ValueError("semantic_level_position_missing")
        return self

class RelationSpec(StrictModel):
    source_rule_id: str
    target_rule_id: str
    source_columns: list[str] = Field(min_length=1)
    target_columns: list[str] = Field(min_length=1)
    # A wide hierarchy has several physical endpoint keys. Selecting a level
    # is a mapping decision, not permission to infer parent/child semantics.
    source_level: int | None = Field(default=None, gt=0)
    target_level: int | None = Field(default=None, gt=0)
    # An entirely NULL endpoint means that an optional relationship is absent.
    # Partial composite keys and non-NULL missing bindings always fail closed.
    require_full_coverage: bool = False
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
    # Legacy explicitly supplied RuleSpecs remain operator-approved input.
    # Generated rules must record a checked decision, not merely nonempty text.
    origin: Literal['user_rule', 'discovery'] = 'user_rule'
    semantic_decision: Literal['unreviewed', 'explicit_declaration', 'model_review'] = 'unreviewed'
    semantic_status: Literal['supported', 'refuted', 'unresolved'] = 'unresolved'
    reviewed_context_refs: list[str] = Field(default_factory=list, max_length=128)
    refuting_refs: list[str] = Field(default_factory=list, max_length=128)
    conditions_satisfied: bool = False

class RuleBudget(StrictModel):
    max_db_cu: float = Field(default=40, gt=0)
    max_rows_returned: int = Field(default=20000, gt=0)
    max_seconds: float = Field(default=120, gt=0)

class OperatorSpec(StrictModel):
    op: str
    args: dict[str, Any] = Field(default_factory=dict)
    output: str = ""

class ConceptComponent(StrictModel):
    """One declared concept; definition identity is independent of row context."""
    role: str = Field(min_length=1, max_length=64, description=
        'Stable business role within this joint context, for example metric, product or channel; '
        'unique among components. This is not the column-role enum. Do not put row values or '
        'combined labels here. Role and definition_ref distinguish concept identities.')
    entity_kind: str = Field(min_length=1, max_length=64, description=
        'Registered business type of this independent concept, distinct from the joint-context '
        'type. It must preserve the definition grain; a repeated name alone does not prove '
        'that two business definitions are the same.')
    column: str = Field(min_length=1, description=
        'Issued column ID in an Agent proposal; authorized physical column name in a compiled '
        'rule. Prefer a separate concept column. This column supplies the concept value, '
        'not the identity of the full source row or path.')
    key_column: str | None = Field(default=None, min_length=1, description=
        'Independent business-code column for this concept. When present, identity uses '
        'this code and column supplies only the label. Same labels with different codes '
        'remain different objects. Null retains the explicitly declared value identity.')
    namespace_columns: list[str] | None = Field(default=None, max_length=16, description=
        'Null inherits the joint-context namespaces. An explicit subset selects this '
        'concept identity scope; [] permits cross-context reuse only with scope_evidence_ref. '
        'Source support permissions still follow the original row context.')
    scope_evidence_ref: str | None = Field(default=None, min_length=1, max_length=256, description=
        'Issued document section establishing the narrower concept identity namespace. '
        'Required whenever namespace_columns differs from the joint-context scope.')
    definition_ref: str = Field(min_length=1, max_length=256, description=
        'Issued definition-section reference exactly as doc_id@revision:section_id. It must '
        'define this concept and its grain; never use a bare name, invented reference or '
        'sample value. Different definitions must not share identity merely because labels match.')
    identity_domain: str | None = Field(default=None, min_length=1, max_length=256, description=
        'Optional explicitly verified business-code domain shared with other entity rules. '
        'Requires key_column and identity_evidence; the same type, domain, namespace values '
        'and code then identify one object across tables. Null keeps definition-local identity.')
    identity_evidence: list[str] = Field(default_factory=list, max_length=16, description=
        'Issued document-section references proving this code belongs to identity_domain. '
        'Equal labels or overlapping codes alone do not establish a common code domain.')
    relation: str = Field(min_length=1, max_length=64, description=
        'Registered relation from the joint-context entity to this concept type. Preserve '
        'the source item binding so separate product and channel edges do not invent combinations.')
    # No default splitting: all segment positions/counts must be declared.
    delimiter: str | None = Field(default=None, min_length=1, max_length=8, description=
        'Null for a separate concept column. Otherwise the exact literal separator explicitly '
        'supported by the cited document, never guessed from punctuation. Declare position '
        'and segment_count together. No regex, trimming or implicit fallback is applied.')
    position: int | None = Field(default=None, ge=0, le=15, description=
        'Zero-based segment position established by the cited definition. Required only with '
        'delimiter and segment_count; must be smaller than segment_count. Null for a separate column.')
    segment_count: int | None = Field(default=None, ge=2, le=16, description=
        'Exact total number of segments established by the cited document. Every projected '
        'value must have this count and nonempty segments; mismatches fail validation instead '
        'of guessing a different split. Null for a separate concept column.')
    @model_validator(mode="after")
    def validate_segment(self):
        if self.identity_domain and (not self.key_column or not self.identity_evidence):
            raise ValueError('concept_identity_domain_evidence_required')
        if self.identity_evidence and not self.identity_domain:
            raise ValueError('concept_identity_domain_required')
        segmented = self.delimiter is not None
        if segmented != (self.position is not None and self.segment_count is not None):
            raise ValueError("concept_segment_contract_required")
        if not segmented and (self.position is not None or self.segment_count is not None):
            raise ValueError("concept_segment_contract_required")
        if segmented and self.position >= self.segment_count:
            raise ValueError("concept_segment_out_of_range")
        return self


class ConceptMappingSpec(StrictModel):
    """Explicit semantic declaration, never inferred from punctuation alone."""
    evidence_ref: str = Field(min_length=1, max_length=256, description=
        'Issued document version exactly as doc_id@revision, without section suffix. The '
        'document must support decomposing this source into independent concepts and retaining '
        'their joint binding; any delimiter, segment order and grain require explicit evidence. '
        'The reference alone grants no semantic approval and must remain a rule dependency.')
    components: list[ConceptComponent] = Field(min_length=2, max_length=16, description=
        'Independent concepts assigned together by one source-item context. Each component '
        'needs its own role, definition and permitted context-to-concept relation. Do not '
        'encode the whole product/channel/metric combination as a single concept identity.')
    @model_validator(mode="after")
    def unique_roles(self):
        if len({c.role for c in self.components}) != len(self.components):
            raise ValueError("duplicate_concept_role")
        return self


class RuleSpec(StrictModel):
    rule_id: str
    revision: int = Field(default=1, ge=1)
    kind: Literal["column_group_entity", "wide_hierarchy", "long_hierarchy", "reference", "concept_mapping"]
    goal_refs: list[str] = Field(default_factory=list)
    semantic_schema: SemanticSchema = Field(default_factory=SemanticSchema)
    source: SourceRef
    scope: ScopeSpec = Field(default_factory=ScopeSpec)
    identity: IdentitySpec
    attributes: AttributeSpec = Field(default_factory=AttributeSpec)
    hierarchy: HierarchySpec | None = None
    relation: RelationSpec | None = None
    concept_mapping: ConceptMappingSpec | None = None
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
        if (self.kind == "concept_mapping") != (self.concept_mapping is not None):
            raise ValueError("concept_mapping_contract_required")
        if self.concept_mapping:
            for component in self.concept_mapping.components:
                scope = component.namespace_columns
                if scope is not None:
                    if len(set(scope)) != len(scope) or not set(scope) <= set(self.scope.namespaces):
                        raise ValueError('concept_namespace_invalid')
                    if set(scope) != set(self.scope.namespaces) and not component.scope_evidence_ref:
                        raise ValueError('concept_scope_evidence_required')
                if component.scope_evidence_ref and component.scope_evidence_ref.rsplit(':',1)[0] not in self.dependencies.documents:
                    raise ValueError('concept_scope_document_required')
                if any(ref.rsplit(':',1)[0] not in self.dependencies.documents for ref in component.identity_evidence):
                    raise ValueError('concept_identity_document_required')
            if self.identity.entity_kind in {c.entity_kind for c in self.concept_mapping.components}:
                raise ValueError("concept_context_type_must_be_distinct")
            if self.hierarchy or self.relation or self.attributes.single_valued or self.attributes.multi_valued:
                raise ValueError("concept_mapping_contract_conflict")
            if self.concept_mapping.evidence_ref not in self.dependencies.documents:
                raise ValueError("concept_mapping_document_required")
            if self.identity.semantic_resolution != "source_scoped":
                raise ValueError("concept_binding_must_be_source_scoped")
        if self.publish.get("mode") != "staged":
            raise ValueError("publication_must_be_staged")
        if self.identity.level_policy == "relative_path" and self.kind != "wide_hierarchy":
            raise ValueError("relative_path_requires_wide_hierarchy")
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
