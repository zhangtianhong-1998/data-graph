from __future__ import annotations
import os
import re
from functools import partial
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

    @property
    def file_scope_revision(self) -> dict:
        """Invalidate artifacts from loaders that ignored file table/column selections."""
        if self.kind in {"excel_workbook", "csv_file"} and (self.allowed_tables or self.allowed_columns):
            return {"file_scope_version": 1}
        return {}

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
    output_token_field: Literal["max_tokens", "max_completion_tokens"] | None = None
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

    @model_serializer(mode='wrap')
    def serialize_options(self, handler):
        data = handler(self)
        if self.output_token_field is None:
            data.pop('output_token_field', None)  # Keep existing workflow input fingerprints.
        return data

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


class InheritedGraphRef(StrictModel):
    """Explicit cross-project graph inheritance reference, independent of isolation_domain."""
    inherit_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    source_project_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    snapshot_path: str | None = Field(default=None, min_length=1)
    expected_graph_digest: str | None = Field(default=None, min_length=1)
    allowed_scopes: list[str] = Field(default_factory=lambda: ["public"], min_length=1, max_length=128)

    @model_validator(mode="after")
    def check_target(self):
        if bool(self.source_project_id) == bool(self.snapshot_path):
            raise ValueError("inherited_graph_source_required")
        return self


class WorkflowPosition(StrictModel):
    """Canvas coordinates for visual workflow DAG orchestration."""
    x: float = 0.0
    y: float = 0.0


class WorkflowUnitConfig(StrictModel):
    """One parallel execution unit (extract node) inside a parent project workflow."""
    unit_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    label: str | None = Field(default=None, max_length=120)
    source_ids: list[str] = Field(default_factory=list, max_length=64)
    source_table_overrides: dict[str, list[str]] = Field(default_factory=dict, max_length=64)
    document_ids: list[str] | None = Field(default=None, max_length=128)
    goal_markdown: str | None = Field(default=None, max_length=32000)
    probe_filter_hint: str | None = Field(default=None, max_length=2000)
    max_calls: int | None = Field(default=None, ge=0)
    max_run_seconds: float | None = Field(default=None, gt=0)
    enable_co_projection: bool | None = None
    position: WorkflowPosition | None = None


class EmbeddingConfig(StrictModel):
    """Configurable, replaceable semantic embedding service for subgraph retrieval."""
    model_config = ConfigDict(hide_input_in_errors=True)
    provider: Literal["none", "openai_compatible"] = "none"
    base_url: str = ""
    model: str = ""
    api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    api_key_env: str = "LIGHTGRAPH_EMBEDDING_API_KEY"
    allow_insecure_http: bool = False
    timeout_seconds: float = Field(default=120.0, gt=0)
    batch_size: int = Field(default=32, ge=1, le=512)
    dimensions: int | None = Field(default=None, gt=0,
        description="Optional vector dimension; auto-detected when null (e.g. 1024 for Qwen3-Embedding-0.6B).")
    query_instruction: str = Field(default="", max_length=1000,
        description="Optional instruction prefix prepended only to retrieval queries (e.g. Qwen3-Embedding Instruct format).")

    @field_validator("provider", mode="before")
    @classmethod
    def migrate_hash_provider(cls, value):
        return "none" if value == "local_hash" else value

    @field_validator("base_url")
    @classmethod
    def normalize_embedding_endpoint(cls, value):
        from .model_endpoint import normalize_embedding_base_url
        return normalize_embedding_base_url(value)

    @field_validator("model", "api_key_env")
    @classmethod
    def normalize_embedding_identifier(cls, value, info):
        value = value.strip()
        if info.field_name == "api_key_env" and not value:
            value = "LIGHTGRAPH_EMBEDDING_API_KEY"
        if info.field_name == "api_key_env" and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError("invalid_embedding_key_environment")
        return value


class FusionSourceBinding(StrictModel):
    source_id: str = Field(min_length=1)
    schema_name: str = Field(alias='schema', min_length=1)
    table: str = Field(min_length=1)
    key_columns: dict[str, str] = Field(min_length=1, max_length=32,
        description="Existing logical identity key name to the physical column in this table.")


class EntityFusionRule(StrictModel):
    """Explicit cross-source identity mapping; never infer identity from labels."""
    entity_type: str = Field(min_length=1, max_length=100)
    canonical_type: str = Field(min_length=1, max_length=100)
    identity_domain: str = Field(min_length=1, max_length=100)
    key_mapping: dict[str, str] = Field(min_length=1, max_length=32,
        description="Canonical identity key name -> existing node identity key name. Every identity key must be mapped; dropping keys is forbidden.")
    key_normalizers: dict[str, list[Literal['trim', 'casefold', 'nfkc']]] = Field(default_factory=dict, max_length=32)
    property_aliases: dict[str, str] = Field(default_factory=dict, max_length=128,
        description="Existing property name -> canonical property name. Use source_id.schema.table.property for a table-specific mapping. Units and meaning must already agree.")
    property_normalizers: dict[str, list[Literal['trim', 'casefold', 'nfkc']]] = Field(default_factory=dict, max_length=128)
    source_bindings: list[FusionSourceBinding] = Field(default_factory=list, max_length=512,
        description="Optional approved tables and complete physical keys for this rule.")

    @model_validator(mode='after')
    def unambiguous_keys(self):
        if (len(set(self.key_mapping.values())) != len(self.key_mapping)
                or any(not value.strip() for pair in self.key_mapping.items() for value in pair)
                or not set(self.key_normalizers) <= set(self.key_mapping)
                or any(not value.strip() for pair in self.property_aliases.items() for value in pair)):
            raise ValueError('invalid_fusion_key_mapping')
        for binding in self.source_bindings:
            if (binding.key_columns.keys() != set(self.key_mapping.values())
                    or len(set(binding.key_columns.values())) != len(binding.key_columns)
                    or not all(s.strip() for s in (binding.source_id, binding.schema_name, binding.table,
                                                    *binding.key_columns.values()))):
                raise ValueError('invalid_fusion_source_binding')
        return self

    @model_serializer(mode='wrap')
    def serialize_rule(self, handler):
        data = handler(self)
        if not self.source_bindings:
            data.pop('source_bindings', None)  # Preserve historical policy and checkpoint fingerprints.
        for binding in data.get('source_bindings', ()):
            if 'schema_name' in binding:
                binding['schema'] = binding.pop('schema_name')
        return data


class FusionPolicyConfig(StrictModel):
    version: Literal[1] = 1
    label_conflict_policy: Literal['retain_variants', 'quarantine', 'strict'] = 'retain_variants'
    entity_rules: list[EntityFusionRule] = Field(default_factory=list, max_length=128)

    @model_validator(mode='after')
    def unambiguous_domains(self):
        signatures = set()
        input_types = {}
        canonical_types = {}
        for rule in self.entity_rules:
            signature = (rule.entity_type, frozenset(rule.key_mapping.values()))
            if signature in signatures:
                raise ValueError('duplicate_fusion_entity_rule')
            signatures.add(signature)
            target = (rule.canonical_type, rule.identity_domain)
            if input_types.setdefault(rule.entity_type, target) != target:
                raise ValueError('incompatible_fusion_identity_contract')
            contract = (rule.identity_domain, tuple(sorted(rule.key_mapping)))
            if canonical_types.setdefault(rule.canonical_type, contract) != contract:
                raise ValueError('incompatible_fusion_identity_contract')
        # A previously canonicalized type must remain stable in downstream merges.
        if any(t in input_types and input_types[t] != (t, contract[0]) for t, contract in canonical_types.items()):
            raise ValueError('incompatible_fusion_identity_contract')
        return self


class ReviewedKeyProjection(StrictModel):
    """One frozen contributor and its two observed columns."""
    contributor_id: str = Field(min_length=1)
    graph_digest: str = Field(pattern='^[0-9a-f]{64}$')
    source_id: str = Field(min_length=1)
    schema_name: str = Field(alias='schema', min_length=1)
    table: str = Field(min_length=1)
    key_column: str = Field(min_length=1)
    name_column: str = Field(min_length=1)


class ReviewedKeyPairRule(StrictModel):
    """A business approval for individual typed key/name observations in two checkpoints."""
    approval_ref: str = Field(min_length=1, max_length=256)
    entity_type: str = Field(min_length=1)
    projections: tuple[ReviewedKeyProjection, ReviewedKeyProjection]
    observation_digests: list[str] = Field(min_length=1, max_length=5000)

    @model_validator(mode='after')
    def bounded_approval(self):
        a, b = self.projections
        if (not self.approval_ref.strip() or not self.entity_type.strip() or a.contributor_id == b.contributor_id
                or any(p.key_column == p.name_column for p in (a, b))
                or not all(re.fullmatch('[0-9a-f]{64}', token) for token in self.observation_digests)
                or len(set(self.observation_digests)) != len(self.observation_digests)):
            raise ValueError('invalid_reviewed_key_pair_rule')
        return self


class ProjectionBinding(StrictModel):
    """A model can select only an issued member, source and incident relation role."""
    contributor_id: str = Field(min_length=1)
    graph_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    entity_type: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    schema_name: str = Field(min_length=1)
    table: str = Field(min_length=1)
    key_columns: tuple[str, ...] = Field(min_length=1, max_length=32)
    relation_type: str = Field(min_length=1)
    end: Literal['source', 'target']
    opposite_key_columns: tuple[str, ...] = Field(min_length=1, max_length=32)


class VerifiedProjectionRule(StrictModel):
    """Finite same-batch observations, checked again during deterministic replay."""
    pattern_id: str = Field(pattern=r'^pattern_[0-9a-f]{24}$')
    bindings: tuple[ProjectionBinding, ...] = Field(min_length=2, max_length=24)
    observation_digests: tuple[str, ...] = Field(min_length=1)
    composition_version: Literal[1, 2, 3] = 1

    @model_serializer(mode='wrap')
    def serialize_observation_rule(self, handler):
        data = handler(self)
        if self.composition_version == 1:
            data.pop('composition_version', None)  # Preserve archived policy digests.
        return data

    @model_validator(mode='after')
    def bound_members(self):
        if (len(set((p.contributor_id, p.entity_type) for p in self.bindings)) != len(self.bindings)
                or len(set(self.observation_digests)) != len(self.observation_digests)
                or not all(re.fullmatch('[0-9a-f]{64}', token) for token in self.observation_digests)):
            raise ValueError('invalid_verified_projection_rule')
        return self


class MergePolicyConfig(StrictModel):
    """Deterministic safety rules for merging parallel unit and inherited graphs."""
    verified_projection_rules: list[VerifiedProjectionRule] = Field(default_factory=list)
    reviewed_key_pairs: list[ReviewedKeyPairRule] = Field(default_factory=list, max_length=64)

    @model_serializer(mode='wrap')
    def serialize_policy(self, handler):
        data = handler(self)
        if not self.verified_projection_rules:
            data.pop('verified_projection_rules', None)
        if not self.reviewed_key_pairs:
            data.pop('reviewed_key_pairs', None)  # Historical checkpoint policy digests.
        for rule in data.get('reviewed_key_pairs', []):
            for binding in rule['projections']:
                if 'schema_name' in binding:
                    binding['schema'] = binding.pop('schema_name')
        return data

    authority_sources: list[str] = Field(default_factory=list, max_length=64)
    conflict_mode: Literal["strict", "quarantine"] = "quarantine"
    shared_identity_domains: dict[str, list[str]] = Field(default_factory=dict, max_length=128)
    shared_entity_types: list[str] = Field(default_factory=list, max_length=128)
    emit_alignment_candidates: bool = True
    fusion_policy: FusionPolicyConfig | None = Field(default=None,
        description="Explicit versioned fusion rules. Null preserves legacy low-level merge behavior; workflow execution inherits the project policy.")


class WorkflowMergeNodeConfig(StrictModel):
    """A merge or merge-and-extract node joining upstream workflow outputs."""
    node_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    label: str | None = Field(default=None, max_length=120)
    merge_mode: Literal["deterministic", "overlap_review", "merge_and_extract"] = "deterministic"
    source_ids: list[str] = Field(default_factory=list, max_length=64)
    source_table_overrides: dict[str, list[str]] = Field(default_factory=dict, max_length=64)
    document_ids: list[str] | None = Field(default=None, max_length=128)
    goal_markdown: str | None = Field(default=None, max_length=32000)
    probe_filter_hint: str | None = Field(default=None, max_length=2000)
    max_calls: int | None = Field(default=None, ge=0)
    max_run_seconds: float | None = Field(default=None, gt=0)
    enable_co_projection: bool | None = None
    merge_policy: MergePolicyConfig | None = None
    position: WorkflowPosition | None = None


class WorkflowEdgeConfig(StrictModel):
    """Directed DAG wire connecting extract -> merge, merge -> merge, or node -> output."""
    edge_id: str = Field(min_length=1, max_length=128)
    source: str = Field(min_length=1, max_length=64)
    target: str = Field(min_length=1, max_length=64)


class WorkflowConfig(StrictModel):
    """Project-level parallel workflow topology and merge policy."""
    enabled: bool = False
    legacy_merge: bool = Field(default=False,
        description="Explicit compatibility mode for old edge-less workflows that implicitly merged all units.")
    max_parallel_workers: int = Field(default=4, ge=1, le=24)
    enable_co_projection: bool = False
    units: list[WorkflowUnitConfig] = Field(default_factory=list, max_length=64)
    merge_nodes: list[WorkflowMergeNodeConfig] = Field(default_factory=list, max_length=16)
    edges: list[WorkflowEdgeConfig] = Field(default_factory=list, max_length=256)
    output_position: WorkflowPosition | None = None
    merge_policy: MergePolicyConfig = Field(default_factory=MergePolicyConfig)

    @model_validator(mode="after")
    def validate_dag(self):
        policies = [self.merge_policy, *(node.merge_policy for node in self.merge_nodes if node.merge_policy)]
        if any(policy.verified_projection_rules for policy in policies):
            raise ValueError('projection_proposals_require_merge_review')
        if any(rule.approval_ref.strip() == 'simulation_only' for policy in policies for rule in policy.reviewed_key_pairs):
            raise ValueError('simulated_key_pairs_cannot_be_published')
        unit_ids = [unit.unit_id for unit in self.units]
        merge_ids = [node.node_id for node in self.merge_nodes]
        if len(set(unit_ids)) != len(unit_ids):
            raise ValueError("duplicate_workflow_unit_id")
        if len(set(merge_ids)) != len(merge_ids):
            raise ValueError("duplicate_workflow_merge_node_id")
        all_ids = unit_ids + merge_ids
        if "output" in all_ids:
            raise ValueError("reserved_workflow_node_id")
        if len(set(all_ids)) != len(all_ids):
            raise ValueError("duplicate_workflow_node_id")
        known_sources = set(all_ids)
        known_targets = set(merge_ids) | {"output"}
        adj: dict[str, list[str]] = {nid: [] for nid in known_sources}
        for edge in self.edges:
            if edge.source not in known_sources:
                raise ValueError("workflow_edge_source_unknown")
            if edge.target not in known_targets:
                raise ValueError("workflow_edge_target_unknown")
            if edge.source == edge.target:
                raise ValueError("workflow_cycle_detected")
            adj[edge.source].append(edge.target)
        # Detect any cycle in the directed graph.
        visiting: set[str] = set()
        visited: set[str] = set()

        def dfs(node: str) -> None:
            if node in visited or node == "output":
                return
            if node in visiting:
                raise ValueError("workflow_cycle_detected")
            visiting.add(node)
            for nxt in adj.get(node, ()):
                dfs(nxt)
            visiting.remove(node)
            visited.add(node)

        for nid in adj:
            dfs(nid)
        return self


class RunConfig(StrictModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    _agent: AgentConfig | None = PrivateAttr(default=None)
    _document_table_bindings: dict[str, str] = PrivateAttr(default_factory=dict)
    agent_config: str | None = Field(default=None, min_length=1)
    isolation_domain: str = "default"
    fusion_policy: FusionPolicyConfig = Field(default_factory=FusionPolicyConfig)
    sources: list[SourceConfig]
    documents: list[str] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)
    goal: ExtractionGoal = Field(default_factory=ExtractionGoal)
    goal_markdown: str | None = Field(default=None, max_length=64000,
        description="User-authored Markdown construction objective. Null renders the legacy goal fields; source authorization remains in typed configuration.")
    limits: QueryLimits = Field(default_factory=QueryLimits)
    model: ModelConfig = Field(default_factory=ModelConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig,
        description="Configurable, replaceable embedding service for subgraph retrieval (local OpenAI-compatible server or offline fallback).")
    output_dir: str = "runs/default"
    record_trace: bool = Field(default=True,
        description="Record the complete local run trajectory with credentials redacted; retain until manually cleared.")
    scopes: list[str] = Field(default_factory=lambda:["public"])
    scope_bindings: list[GraphScopeBinding] = Field(default_factory=list, max_length=1024,
        description='Operator-owned source/table columns determining each result support scope. No binding means public; never inferred from identity keys.')
    max_working_bytes: int = Field(default=32*1024*1024, gt=0)
    max_db_queries: StrictInt | None = Field(default=None, gt=0,
        description="Total source-query admissions, including metadata and transaction controls. Null removes only the query-count ceiling.")
    max_returned_rows: StrictInt | None = Field(default=400000, gt=0,
        description="Cumulative rows returned by all source queries across the run, including exploration and validation. Null removes only this cumulative row ceiling.")
    max_returned_bytes: StrictInt | None = Field(default=64*1024*1024, gt=0,
        description="Cumulative estimated row bytes returned by all source queries across the run, not resident memory, disk space, model tokens or MCP traffic. Null removes only this cumulative byte ceiling.")
    construction_target: Literal["instances", "structure"] = Field(default="instances",
        description="Build concrete instance nodes and edges, or publish only structure and source mappings with no claim of instance validation.")
    enable_co_projection: bool = Field(default=False,
        description="Opt-in cardinality-guarded co-projection across mappings on the same table and predicate, with automatic narrow fallback.")
    inherited_graphs: list[InheritedGraphRef] = Field(default_factory=list, max_length=16,
        description="Explicitly authorized published graphs inherited from prior projects or snapshots without re-scanning old sources.")
    workflow: WorkflowConfig | None = Field(default=None,
        description="Optional in-project parallel workflow units and deterministic merge policy.")
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
        if len({g.inherit_id for g in self.inherited_graphs}) != len(self.inherited_graphs):
            raise ValueError("inherit_ids_must_be_unique")
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
        for ref in self.inherited_graphs:
            if ref.snapshot_path:
                ref.snapshot_path = str((base / ref.snapshot_path).resolve())
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


def run_query_budget(config):
    """Create the configured whole-run source budget; phase changes never reset it."""
    from ..sources.budget import QueryBudget
    return QueryBudget(max_queries=config.max_db_queries, max_run_seconds=config.max_run_seconds,
        max_returned_rows=config.max_returned_rows, max_returned_bytes=config.max_returned_bytes)


def install_run_query_budget(config, adapters):
    """Share limits across borrowed adapters while importing prior usage exactly once.

    Preparing sources may already have consumed metadata queries. Retain that
    consumption and the earliest deadline, including an existing cancellation.
    """
    budget = run_query_budget(config)
    for adapter in adapters:
        adapter.install_budget(budget)
    return budget


def open_sources(config, *, metadata_only=False, source_credentials=None, isolate_database_failures=False):
    from ..sources import ExcelSource, CSVSource, GaussSource, MySQLSource
    from ..sources.base import QueryStats
    from ..sources.reconnect import open_database_source
    sources=[]
    budget=run_query_budget(config)
    try:
        for s in config.sources:
            if s.kind=="excel_workbook":
                if not s.path:raise LightGraphError("config_invalid")
                sources.append(ExcelSource(s.path,source_id=s.id,schema_name=s.schema_name,limits=config.limits,
                    header_row=s.header_row,include_hidden=s.include_hidden,query_budget=budget,
                    max_memory_bytes=config.max_working_bytes,metadata_only=metadata_only,
                    allowed_tables=s.allowed_tables or None,allowed_columns=s.allowed_columns))
            elif s.kind=="csv_file":
                sources.append(CSVSource(s.path,source_id=s.id,limits=config.limits,
                    encoding=s.encoding,delimiter=s.delimiter,header=s.header,
                    null_values=s.null_values,type_inference=s.type_inference,
                    columns=s.columns,table_name=s.table_name,schema_name=s.schema_name,
                    header_row=s.header_row,max_memory_bytes=config.max_working_bytes,
                    query_budget=budget,metadata_only=metadata_only,
                    allowed_tables=s.allowed_tables or None,allowed_columns=s.allowed_columns))
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
                stats = QueryStats()
                options['_stats'] = stats
                open_adapter = partial(adapter, dsn, source_id=s.id, allowed_tables=s.allowed_tables,
                    allowed_columns=s.allowed_columns, limits=config.limits, verified_capabilities=verified,
                    query_budget=budget, **options)
                try:
                    sources.append(open_database_source(open_adapter, budget,
                        engine='mysql' if s.kind == 'mysql_database' else 'gauss'))
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
                    sources.append(UnavailableSource(s, exc, limits=config.limits, query_budget=budget, stats=stats))
        return sources
    except Exception:
        for s in sources:s.close()
        raise
