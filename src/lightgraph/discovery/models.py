"""Closed discovery proposals: model output never controls validation status."""
from __future__ import annotations
from typing import Annotated, Literal, Union
from pydantic import Field, model_validator
from lightgraph.contracts import StrictModel, RuleSpec, SourceRef, TableSpec, digest
from lightgraph.semantic_schema import SemanticSchema

Role = Literal['identity_code','namespace','label','alias','semantic_attribute','display_order',
               'level_position','fact_value','relation_endpoint','discriminator','audit','unknown']
Task = Literal['T0_discovery','T1_table_discovery','T2_cross_table','T3_hierarchy','T4_document','T5_revision']
# The project catalog validates meaning, type and allowed entities. Parsing a
# proposed name does not register it or grant permission to persist values.
SemanticProperty = Annotated[str, Field(min_length=1, max_length=64)]

class Member(StrictModel):
    column_id: str
    role: Role
    in_identity: bool = False
    multi_value: bool = False
    exclude_from_projection: bool = False
    property_name: SemanticProperty | None = None
    @model_validator(mode='after')
    def guard(self):
        if self.role in ('display_order','audit','fact_value') and self.in_identity:
            raise ValueError('excluded_role_in_identity')
        return self

class ColumnGroup(StrictModel):
    group_id: str
    entity_kind_hint: str
    members: list[Member] = Field(min_length=1, max_length=64)
    identity_resolution: Literal['source_scoped', 'shared_definition'] = 'source_scoped'

class KeyAttributes(StrictModel):
    single_valued: list[str] = Field(default_factory=list)
    multi_valued: list[str] = Field(default_factory=list)
    display_only: list[str] = Field(default_factory=list)
    # Issued column ID -> registered property, never guessed from a field suffix.
    property_names: dict[str, SemanticProperty] = Field(default_factory=dict)

class EntityKey(StrictModel):
    group_id: str
    entity_kind_hint: str
    key_columns: list[str] = Field(min_length=1, max_length=16)
    attributes: KeyAttributes
    namespace_columns: list[str] = Field(default_factory=list, max_length=16)
    level_policy: Literal['not_applicable','level_qualified','code_with_fill_merge'] = 'not_applicable'
    normalization: Literal['identity'] = 'identity'
    identity_resolution: Literal['source_scoped', 'shared_definition'] = 'source_scoped'
    @model_validator(mode='after')
    def guard(self):
        if len(set(self.key_columns)) != len(self.key_columns): raise ValueError('duplicate_key_column')
        return self

class HypothesisLevel(StrictModel):
    position: int = Field(gt=0)
    key: str
    label: str | None = None
    alias: str | None = None
    display_order: str | None = None

class WideHierarchy(StrictModel):
    group_id: str
    entity_kind_hint: str = 'ReportItem'
    levels: list[HypothesisLevel] = Field(min_length=2, max_length=16)
    repeat_policy: Literal['none','collapse_consecutive_equal_identity'] = 'none'
    gap_policy: Literal['split','unresolved'] = 'split'
    multiple_parent_policy: Literal['report_as_dag_candidate'] = 'report_as_dag_candidate'
    cycle_policy: Literal['reject_affected_edges'] = 'reject_affected_edges'
    code_relation: Literal['unknown','strict_prefix','set_inclusion'] = 'unknown'
    ordering_evidence: list[str] = Field(default_factory=list)
    namespace_columns: list[str] = Field(default_factory=list, max_length=16)
    path_semantics: Literal["legacy", "relative_path"] = "legacy"
    identity_domain: str = ""
    identity_evidence: list[str] = Field(default_factory=list, max_length=16)
    attribute_evidence: list[str] = Field(default_factory=list, max_length=16)
    identity_scope: Literal["namespace_local", "domain_global"] = "namespace_local"
    global_scope_evidence: list[str] = Field(default_factory=list, max_length=16)
    taxonomy: str = ""
    taxonomy_evidence: list[str] = Field(default_factory=list, max_length=16)
    position_semantics: Literal["ordinal_slots", "ordered_slots"] | None = None
    position_evidence: list[str] = Field(default_factory=list, max_length=16)
    context_qualifiers: dict[str, str] = Field(default_factory=dict)
    context_evidence: list[str] = Field(default_factory=list, max_length=16)
    allow_dag: bool = False
    dag_evidence: list[str] = Field(default_factory=list, max_length=16)
    @model_validator(mode='after')
    def guard(self):
        if len({l.position for l in self.levels}) != len(self.levels): raise ValueError('duplicate_hierarchy_position')
        return self

class LongHierarchy(StrictModel):
    group_id: str
    entity_kind_hint: str = 'DimensionMember'
    key_column: str
    parent_column: str
    label_column: str | None = None
    alias_columns: list[str] = Field(default_factory=list,max_length=2)
    namespace_columns: list[str] = Field(default_factory=list,max_length=16)
    ordering_evidence: list[str] = Field(default_factory=list,max_length=16)

class Reference(StrictModel):
    source_entity: str | None = Field(default=None,description='Verified source entity rule ID from entity_bindings.')
    source_columns: list[str] = Field(min_length=1,max_length=16,description='Issued column IDs on the CURRENT table, ordered to match source_entity key_columns.')
    target_entity: str
    source_level: int | None = Field(default=None,gt=0,description='Required selected level for a wide-hierarchy source binding; omit for ordinary groups and long hierarchies.')
    target_level: int | None = Field(default=None,gt=0,description='Required selected level for a wide-hierarchy target binding; omit for ordinary groups and long hierarchies.')
    target_columns: list[str] = Field(min_length=1,max_length=16,description='Issued foreign-key column IDs on the CURRENT table, ordered to match target_entity key_columns. Do not use remote-table IDs.')
    direction: Literal['source_to_target'] = 'source_to_target'
    cardinality_hint: Literal['n_to_1','1_to_n','n_to_m','1_to_1','unknown'] = 'unknown'
    condition_ref: str | None = None
    polymorphic_by: str | None = None
    require_full_coverage: bool = False
    normalization: Literal['identity'] = 'identity'
    relation_kind: str = Field(default='verified_reference',min_length=1,max_length=64)
    @model_validator(mode='after')
    def guard(self):
        # Each endpoint matches its own registered key, whose arity can differ.
        if len(set(self.source_columns))!=len(self.source_columns) or len(set(self.target_columns))!=len(self.target_columns):
            raise ValueError('duplicate_reference_column')
        return self

class DiscriminatorPartition(StrictModel):
    selector_ref: str
    applies_to_entity: str
    source_value_column: str
class Discriminator(StrictModel):
    discriminating_column: str
    partitions: list[DiscriminatorPartition] = Field(min_length=1,max_length=64)
    unpartitioned_rule_policy: Literal['reject'] = 'reject'
    unknown_partition_policy: Literal['unresolved'] = 'unresolved'
class ExclusionItem(StrictModel):
    column_id: str
    reason: Literal['display_order','audit_metadata','observation_value','sensitive','out_of_goal']
    affects: list[Literal['identity','projection']]
class Exclusion(StrictModel):
    exclusions: list[ExclusionItem] = Field(min_length=1,max_length=64)
    not_excluded: list[str] = Field(default_factory=list)
    evidence_requirement: Literal['check_fd_or_display_order_probe'] = 'check_fd_or_display_order_probe'
    @model_validator(mode='after')
    def guard(self):
        if set(self.not_excluded) & {x.column_id for x in self.exclusions}: raise ValueError('exclusion_conflict')
        return self

class ProposalBase(StrictModel):
    evidence_cited: list[str] = Field(min_length=1,max_length=32)
    confidence_band: Literal['low','medium','high'] = 'low'
    rationale: str = Field(default='',max_length=600)
    requested_probes: list[Literal['profile_column_group','check_fd','distinct_projection',
        'check_reference','probe_level_groups','probe_path_consistency','probe_repeated_fill',
        'detect_cycle','check_discriminator','sample_stratified','profile_columns']] = Field(default_factory=list,max_length=6)
class ColumnGroupProposal(ProposalBase):
    kind: Literal['column_group']
    claim: ColumnGroup
class EntityKeyProposal(ProposalBase):
    kind: Literal['entity_key']
    claim: EntityKey
class HierarchyProposal(ProposalBase):
    kind: Literal['wide_hierarchy']
    claim: WideHierarchy
class LongHierarchyProposal(ProposalBase):
    kind: Literal['long_hierarchy']
    claim: LongHierarchy
class ReferenceProposal(ProposalBase):
    kind: Literal['reference']
    claim: Reference
class DiscriminatorProposal(ProposalBase):
    kind: Literal['discriminator']
    claim: Discriminator
class ExclusionProposal(ProposalBase):
    kind: Literal['exclusion']
    claim: Exclusion
Proposal = Annotated[Union[ColumnGroupProposal,EntityKeyProposal,HierarchyProposal,LongHierarchyProposal,ReferenceProposal,
                           DiscriminatorProposal,ExclusionProposal],Field(discriminator='kind')]
class ProposalBatch(StrictModel):
    packet_version: Literal['1.0'] = '1.0'
    task: Task
    semantic_schema: SemanticSchema = Field(default_factory=SemanticSchema)
    proposals: list[Proposal] = Field(default_factory=list,max_length=6)
    open_questions: list[str] = Field(default_factory=list,max_length=12)
    abstain: bool = False
    notes: str = Field(default='',max_length=600)
    @model_validator(mode='after')
    def guard(self):
        allowed = {'T0_discovery': {'column_group','entity_key','exclusion','wide_hierarchy','long_hierarchy'},
                   'T1_table_discovery': {'column_group','entity_key','exclusion'},
                   'T2_cross_table': {'reference','discriminator'}, 'T3_hierarchy': {'wide_hierarchy','long_hierarchy'},
                   'T4_document': set(), 'T5_revision': {'column_group','entity_key','exclusion','reference','discriminator','wide_hierarchy','long_hierarchy'}}
        if self.abstain and self.proposals: raise ValueError('abstain_with_proposals')
        if any(p.kind not in allowed[self.task] for p in self.proposals): raise ValueError('task_kind_forbidden')
        return self

class Hypothesis(StrictModel):
    hypothesis_id: str
    kind: str
    source: SourceRef
    # Authorized schema references only. Explanation/sample strings are not retained.
    columns: list[str]
    status: Literal['proposed','probing','supported','counterexample','revised','validated','unresolved','rejected','deferred','stale'] = 'proposed'
    evidence_level: Literal['L0_declared','L1_observed','L2_sampled','L3_full_verified','L4_published'] = 'L0_declared'
    created_by: Literal['rule','llm','user'] = 'rule'
    revision: int = Field(default=1,ge=1,le=3)
    revision_of: str | None = None
    rule_id: str | None = None
    dependencies: dict[str,str] = Field(default_factory=dict)
    codes: list[str] = Field(default_factory=list)
    @staticmethod
    def identity(source, kind, columns): return 'h_' + digest(source.model_dump(mode='json',by_alias=True),kind,columns)[:24]

class ColumnUnderstanding(StrictModel):
    name: str
    declared_type: str
    comment_quality: Literal['empty','echo','generic','informative']
    inferred_role: Role = 'unknown'
    observed_counts: dict[str,int] = Field(default_factory=dict)
    conflicts: list[str] = Field(default_factory=list)
class DataUnderstanding(StrictModel):
    source: SourceRef
    dependency_hash: str
    columns: list[ColumnUnderstanding]
    coverage: Literal['sampled','full','metadata_only'] = 'sampled'
    derived: Literal[True] = True
    rule_refs: list[str] = Field(default_factory=list)
    document_refs: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
class DiscoveryResult(StrictModel):
    rules: list[RuleSpec] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    understandings: list[DataUnderstanding] = Field(default_factory=list)
    issues: list[dict[str,str|int]] = Field(default_factory=list)
    stats: dict[str,int|float|str] = Field(default_factory=dict)
    budget: dict = Field(default_factory=dict)

class PacketColumn(StrictModel):
    id: str
    name: str
    type: str = 'text'
    comment: str = Field(default='',max_length=4000)
    observed: dict[str,int|float|None] = Field(default_factory=dict)
class MetadataDigest(StrictModel):
    columns: list[PacketColumn] = Field(default_factory=list,max_length=256)
class PacketProbe(StrictModel):
    probe_id: str
    operator: str
    aggregates: dict[str,int|float|bool|None] = Field(default_factory=dict)
class PacketStratum(StrictModel):
    stratum: str
    selection: str = 'bounded_sample'
    count: int | None = Field(default=None,ge=0,le=32)
    rows: list[dict[str,str|int|float|bool|None]] = Field(default_factory=list,max_length=32)
class PacketToolObservation(StrictModel):
    """Bounded query evidence for this discovery/review call; never persisted."""
    probe_id: str
    columns: list[str] = Field(max_length=32)
    rows: list[list[str|int|float|bool|None]] = Field(default_factory=list,max_length=8,repr=False)
    truncated: bool = False
    full_source_validation: Literal[False] = False
class PacketDocument(StrictModel):
    doc_id: str
    revision: str
    section_id: str
    heading: str
    locator: str
    text: str = Field(repr=False)
    purpose: Literal['data_description','analysis_knowledge','mixed','unresolved'] = 'data_description'
    scope: dict = Field(default_factory=dict)
    sources: list[SourceRef] = Field(default_factory=list)
    read_state: Literal['complete'] = 'complete'
    evidence_role: Literal['definition_evidence','exploration_guidance'] = 'definition_evidence'
class PacketConflict(StrictModel):
    code: str
    count: int = Field(default=0,ge=0)
    probe_id: str | None = None
class PacketBudget(StrictModel):
    max_hypotheses: int = Field(default=6,gt=0,le=6)
    max_probe_requests: int = Field(default=4,gt=0,le=6)
    max_rationale_chars: int = Field(default=600,gt=0,le=600)
class EntityBinding(StrictModel):
    rule_id: str
    source: SourceRef
    entity_kind: str
    key_columns: list[str]
    level: int | None = Field(default=None,gt=0)

class ProposalRepair(StrictModel):
    """A rejected schema-only proposal and a compiler-issued repair instruction."""
    code: str
    guidance: str = Field(max_length=700)
    proposal: Proposal

class TaskPacket(StrictModel):
    packet_version: Literal['1.0'] = '1.0'
    task: Task
    scope: SourceRef | None = None
    goal: dict = Field(default_factory=dict)
    iteration_context: dict = Field(default_factory=dict)
    semantic_schema: SemanticSchema = Field(default_factory=SemanticSchema)
    subtask: str | None = Field(default=None,max_length=2000)
    context_tables: list[SourceRef] = Field(default_factory=list,max_length=64)
    document_refs: list[dict] = Field(default_factory=list,max_length=128)
    metadata_digest: MetadataDigest = Field(default_factory=MetadataDigest)
    probe_digest: list[PacketProbe] = Field(default_factory=list,max_length=96)
    strata_samples: list[PacketStratum] = Field(default_factory=list,max_length=8)
    tool_observations: list[PacketToolObservation] = Field(default_factory=list,max_length=8,repr=False)
    document_snippets: list[PacketDocument] = Field(default_factory=list,max_length=32)
    known_conflicts: list[PacketConflict] = Field(default_factory=list,max_length=32)
    budget: PacketBudget = Field(default_factory=PacketBudget)
    authorized_entities: list[str] = Field(default_factory=list,max_length=64)
    authorized_selectors: list[str] = Field(default_factory=list,max_length=64)
    revision_of: str | None = None
    allowed_revision_codes: list[str] = Field(default_factory=list,max_length=32)
    revision_candidate: RuleSpec | None = None
    proposal_repair_round: int = Field(default=0,ge=0,le=2)
    proposal_repairs: list[ProposalRepair] = Field(default_factory=list,max_length=6)
    # Schema only. Query rows and tool transcripts never enter a stored packet.
    catalog: list[TableSpec] = Field(default_factory=list,max_length=64)
    entity_bindings: list[EntityBinding] = Field(default_factory=list,max_length=64)
    @model_validator(mode='after')
    def bounds(self):
        from lightgraph.contracts import canonical,ExtractionGoal
        if self.goal:ExtractionGoal.model_validate(self.goal)
        column_ids={c.id for c in self.metadata_digest.columns}
        if len(column_ids)!=len(self.metadata_digest.columns):raise ValueError('duplicate_packet_column_id')
        total=0
        for stratum in self.strata_samples:
            if stratum.count is not None and stratum.count!=len(stratum.rows):raise ValueError('sample_count_mismatch')
            for row in stratum.rows:
                if set(row)-column_ids:raise ValueError('unauthorized_sample_column')
                if any(isinstance(v,str) and len(v.encode())>200 for v in row.values()):raise ValueError('sample_value_too_large')
                total+=len(canonical(row).encode())
        probes={p.probe_id for p in self.probe_digest}
        for observation in self.tool_observations:
            if observation.probe_id not in probes:raise ValueError('unissued_observation_probe')
            if len(set(observation.columns))!=len(observation.columns):raise ValueError('ambiguous_observation_columns')
            for row in observation.rows:
                if len(row)!=len(observation.columns):raise ValueError('observation_row_shape_invalid')
                if any(isinstance(v,str) and len(v.encode())>200 for v in row):raise ValueError('sample_value_too_large')
            total+=len(canonical(observation.model_dump()).encode())
        if total>24576:raise ValueError('sample_packet_too_large')
        if sum(len(d.text.encode()) for d in self.document_snippets)>12000:raise ValueError('document_context_too_large')
        return self
