"""Closed discovery proposals: model output never controls validation status."""
from __future__ import annotations
from typing import Annotated, Literal, Union
from pydantic import Field, model_validator
from lightgraph.contracts import StrictModel, RuleSpec, SourceRef, TableSpec, digest

Role = Literal['identity_code','namespace','label','alias','semantic_attribute','display_order',
               'level_position','fact_value','relation_endpoint','discriminator','audit','unknown']
Task = Literal['T0_discovery','T1_table_discovery','T2_cross_table','T3_hierarchy','T4_document','T5_revision']
SemanticProperty = Literal['label','alias','english_name','description','definition','unit','formula']

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
    target_columns: list[str] = Field(min_length=1,max_length=16,description='Issued foreign-key column IDs on the CURRENT table, ordered to match target_entity key_columns. Do not use remote-table IDs.')
    direction: Literal['source_to_target'] = 'source_to_target'
    cardinality_hint: Literal['n_to_1','1_to_1','unknown'] = 'unknown'
    condition_ref: str | None = None
    polymorphic_by: str | None = None
    require_full_coverage: bool = False
    normalization: Literal['identity'] = 'identity'
    relation_kind: Literal['verified_reference','uses_measure','member_of'] = 'verified_reference'
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
class PacketDocument(StrictModel):
    doc_id: str
    revision: str
    section_id: str
    heading: str
    locator: str
    text: str = Field(repr=False)
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

class TaskPacket(StrictModel):
    packet_version: Literal['1.0'] = '1.0'
    task: Task
    scope: SourceRef | None = None
    goal: dict = Field(default_factory=dict)
    metadata_digest: MetadataDigest = Field(default_factory=MetadataDigest)
    probe_digest: list[PacketProbe] = Field(default_factory=list,max_length=96)
    strata_samples: list[PacketStratum] = Field(default_factory=list,max_length=8)
    document_snippets: list[PacketDocument] = Field(default_factory=list,max_length=32)
    known_conflicts: list[PacketConflict] = Field(default_factory=list,max_length=32)
    budget: PacketBudget = Field(default_factory=PacketBudget)
    authorized_entities: list[str] = Field(default_factory=list,max_length=64)
    authorized_selectors: list[str] = Field(default_factory=list,max_length=64)
    revision_of: str | None = None
    allowed_revision_codes: list[str] = Field(default_factory=list,max_length=32)
    revision_candidate: RuleSpec | None = None
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
        if total>24576:raise ValueError('sample_packet_too_large')
        if sum(len(d.text.encode()) for d in self.document_snippets)>12000:raise ValueError('document_context_too_large')
        return self
