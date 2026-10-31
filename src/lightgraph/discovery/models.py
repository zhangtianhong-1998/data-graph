"""Closed discovery proposals: model output never controls validation status."""
from __future__ import annotations
from typing import Annotated, Literal, Union
from pydantic import ConfigDict, Field, PrivateAttr, model_validator
from lightgraph.contracts import StrictModel, RuleSpec, SourceRef, TableSpec, ConceptMappingSpec, digest
from lightgraph.semantic_schema import SemanticSchema

Role = Literal['identity_code','namespace','label','alias','semantic_attribute','display_order',
               'level_position','fact_value','relation_endpoint','discriminator','audit','unknown']
MAX_HIERARCHY_IDENTITY_REQUESTS = 12
# Transient model reasoning remains verbatim; transport byte budgets still
# bound the whole response and any subsequent request containing this text.
MAX_TRANSIENT_TEXT_CHARS = 8192
Task = Literal['T0_discovery','T1_table_discovery','T2_cross_table','T3_hierarchy','T4_document','T5_revision']
TASK_PROPOSAL_KINDS = {
    'T0_discovery':frozenset({'column_group','entity_key','exclusion','wide_hierarchy','long_hierarchy'}),
    'T1_table_discovery':frozenset({'column_group','entity_key','exclusion','wide_hierarchy','long_hierarchy'}),
    'T2_cross_table':frozenset({'reference','discriminator'}),
    'T3_hierarchy':frozenset({'wide_hierarchy','long_hierarchy'}),
    'T4_document':frozenset(),
    'T5_revision':frozenset({'column_group','entity_key','exclusion','reference','discriminator',
                            'wide_hierarchy','long_hierarchy'}),
}
for _task in ('T0_discovery', 'T1_table_discovery', 'T5_revision'):
    TASK_PROPOSAL_KINDS[_task] = TASK_PROPOSAL_KINDS[_task] | {'concept_mapping'}


def _proposal_batch_schema_guards(schema):
    """Publish the same cross-field constraints used by the runtime validator."""
    schema['allOf']=[{
        'if':{'properties':{'task':{'const':task}},'required':['task']},
        'then':{'properties':{'proposals':(
            {'items':{'properties':{'kind':{'enum':sorted(kinds)}}}} if kinds else {'maxItems':0})}},
    } for task,kinds in TASK_PROPOSAL_KINDS.items()]
    schema['allOf'].append({
        'if':{'properties':{'abstain':{'const':True}},'required':['abstain']},
        'then':{'properties':{'proposals':{'maxItems':0}}},
    })

# The project catalog validates meaning, type and allowed entities. Parsing a
# proposed name does not register it or grant permission to persist values.
SemanticProperty = Annotated[str, Field(min_length=1, max_length=64)]

class Member(StrictModel):
    column_id: str = Field(description='Issued column reference, not a sample value or invented field name.')
    role: Role = Field(description=
        'Column function from this enum, not a business object type: identity_code identifies '
        'an object; label names it; semantic_attribute supplies a registered property; '
        'fact_value is an observation. Do not translate or invent role values.')
    in_identity: bool = False
    multi_value: bool = False
    exclude_from_projection: bool = False
    property_name: SemanticProperty | None = Field(default=None, description=
        'Property identifier from the semantic schema. A new property needs an explicit '
        'compatible definition; a source value or display label is not a property identifier.')
    @model_validator(mode='after')
    def guard(self):
        if self.role in ('display_order','audit','fact_value') and self.in_identity:
            raise ValueError('excluded_role_in_identity')
        return self

class IdentityDomainClaim(StrictModel):
    """Explicit code-domain sharing is a reviewed claim, never inferred from labels."""
    identity_resolution: Literal['source_scoped', 'shared_definition', 'verified_identity_domain'] = 'source_scoped'
    identity_domain: str = Field(default='', max_length=256, description=
        'Business-code domain shared only when independent evidence proves identical meaning across sources.')
    identity_evidence: list[str] = Field(default_factory=list, max_length=16, description=
        'Complete definition-section IDs doc_id@revision:section_id applicable to this table. '
        'Select reference_options.definition_sections; probes and bare document IDs do not prove a shared code domain.')
    attribute_evidence: list[str] = Field(default_factory=list, max_length=16, description=
        'Complete applicable definition-section IDs doc_id@revision:section_id for shared attribute meaning.')
    identity_scope: Literal['namespace_local', 'domain_global'] = 'namespace_local'
    global_scope_evidence: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode='after')
    def check_domain_claim(self):
        if self.identity_resolution == 'verified_identity_domain':
            if not self.identity_domain.strip() or not self.identity_evidence:
                raise ValueError('verified_identity_domain_evidence_required')
            if self.identity_scope == 'domain_global' and not self.global_scope_evidence:
                raise ValueError('global_identity_scope_evidence_required')
        elif self.identity_domain or self.identity_evidence or self.attribute_evidence or self.global_scope_evidence or self.identity_scope != 'namespace_local':
            raise ValueError('identity_domain_policy_conflict')
        return self


class ColumnGroup(IdentityDomainClaim):
    group_id: str
    entity_kind_hint: str = Field(description=
        'Business entity type identifier from the goal or semantic schema, not a column '
        'role or source value. New types require a definition; naming a type does not confirm it.')
    members: list[Member] = Field(min_length=1, max_length=64)

class KeyAttributes(StrictModel):
    single_valued: list[str] = Field(default_factory=list)
    multi_valued: list[str] = Field(default_factory=list)
    display_only: list[str] = Field(default_factory=list)
    # Issued column ID -> registered property, never guessed from a field suffix.
    property_names: dict[str, SemanticProperty] = Field(default_factory=dict)

class EntityKey(IdentityDomainClaim):
    group_id: str
    entity_kind_hint: str = Field(description=
        'Business entity type identifier from the goal or semantic schema. New types '
        'require a definition; never replace business meaning merely to satisfy the schema.')
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
    aliases: list[str] = Field(default_factory=list,max_length=16,description='Additional issued alias/name columns; no identity meaning.')
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
    # Keep an unsupported policy in its candidate so one contract error does
    # not discard valid siblings or silently erase the requested meaning.
    identity_resolution: str | None = Field(default=None, max_length=64,
        description='Optional explicit identity policy: source_scoped for legacy paths, '
        'verified_identity_domain for relative_path. namespace_local limits namespace '
        'values within a verified domain and is not source isolation. Unsupported or '
        'inconsistent policies require proposal repair and independent semantic review.')
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

class ConceptMappingClaim(ConceptMappingSpec):
    group_id: str
    entity_kind_hint: str = Field(description='Registered type for the joint context, distinct from every concept type.')
    key_columns: list[str] = Field(min_length=1, max_length=16, description='Issued source-item identity column IDs.')
    namespace_columns: list[str] = Field(default_factory=list, max_length=16)


class ProposalBase(StrictModel):
    evidence_cited: list[str] = Field(min_length=1,max_length=32)
    confidence_band: Literal['low','medium','high'] = 'low'
    rationale: str = Field(default='',max_length=MAX_TRANSIENT_TEXT_CHARS)
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
class ConceptMappingProposal(ProposalBase):
    kind: Literal['concept_mapping']
    claim: ConceptMappingClaim
Proposal = Annotated[Union[ColumnGroupProposal,EntityKeyProposal,HierarchyProposal,LongHierarchyProposal,ReferenceProposal,
                           DiscriminatorProposal,ExclusionProposal,ConceptMappingProposal],Field(discriminator='kind')]
class ProposalBatch(StrictModel):
    _runtime_pending_codes: list[str] = PrivateAttr(default_factory=list)
    _runtime_invalid_drafts: int = PrivateAttr(default=0)
    _runtime_invalid_codes: dict[str,int] = PrivateAttr(default_factory=dict)
    _runtime_invalid_diagnostics: list[dict] = PrivateAttr(default_factory=list)
    _runtime_object_models: list[dict] = PrivateAttr(default_factory=list)
    model_config = ConfigDict(json_schema_extra=_proposal_batch_schema_guards)
    packet_version: Literal['1.0'] = '1.0'
    task: Task
    semantic_schema: SemanticSchema = Field(default_factory=SemanticSchema)
    proposals: list[Proposal] = Field(default_factory=list,max_length=6)
    open_questions: list[str] = Field(default_factory=list,max_length=12)
    abstain: bool = False
    notes: str = Field(default='',max_length=MAX_TRANSIENT_TEXT_CHARS)
    @model_validator(mode='after')
    def guard(self):
        if self.abstain and self.proposals: raise ValueError('abstain_with_proposals')
        if any(p.kind not in TASK_PROPOSAL_KINDS[self.task] for p in self.proposals): raise ValueError('task_kind_forbidden')
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
class PendingCandidate(StrictModel):
    """Value-free review work; a candidate never grants publication authority."""
    candidate_id: str = Field(pattern=r'^candidate_[a-f0-9]{24}$')
    source: SourceRef
    proposal_kind: Literal['column_group','entity_key','wide_hierarchy','long_hierarchy','reference','concept_mapping']
    failure_code: Literal['semantic_claim_refuted','semantic_claim_unresolved',
        'semantic_condition_unresolved','semantic_review_missing_support','semantic_review_incomplete_context',
        'semantic_revision_no_progress','semantic_revision_constraints_changed',
        'identity_domain_evidence_unresolved','excluded_column_projected',
        'concept_scope_evidence_required','concept_scope_document_required',
        'concept_identity_document_required','concept_namespace_invalid',
        'concept_context_type_must_be_distinct','concept_mapping_contract_conflict',
        'concept_mapping_document_required','concept_binding_must_be_source_scoped',
        'duplicate_concept_relation','concept_identity_domain_evidence_required',
        'concept_identity_domain_required']
    column_refs: list[str] = Field(default_factory=list, max_length=256)
    document_refs: list[str] = Field(default_factory=list, max_length=128)
    rule_refs: list[str] = Field(default_factory=list, max_length=64)
    blocking_evidence_refs: list[str] = Field(default_factory=list, max_length=128)
    required_evidence_refs: list[str] = Field(default_factory=list, max_length=128)
    review_stage: Literal['entity_granularity','entity_definition','hierarchy','reference','concept_mapping']
    status: Literal['pending'] = 'pending'


class DiscoveryResult(StrictModel):
    rules: list[RuleSpec] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    understandings: list[DataUnderstanding] = Field(default_factory=list)
    issues: list[dict[str,str|int]] = Field(default_factory=list)
    stats: dict[str,int|float|str] = Field(default_factory=dict)
    budget: dict = Field(default_factory=dict)
    relation_table_outcomes: dict[str, Literal['resolved','unresolved','deferred']] = Field(default_factory=dict)
    pending_candidates: list[PendingCandidate] = Field(default_factory=list)
    resolved_candidate_ids: list[str] = Field(default_factory=list)
    candidate_replacement_rules: dict[str,list[str]] = Field(default_factory=dict)

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
    aggregates: dict[str,int|float|bool|None|dict[str,int|float|bool|None]] = Field(default_factory=dict)
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
    read_state: Literal['complete','partial'] = 'complete'
    evidence_role: Literal['definition_evidence','exploration_guidance'] = 'definition_evidence'
class PacketConflict(StrictModel):
    code: str
    count: int = Field(default=0,ge=0)
    probe_id: str | None = None
class PacketBudget(StrictModel):
    max_hypotheses: int = Field(default=6,gt=0,le=6)
    max_probe_requests: int = Field(default=4,gt=0,le=6)
    max_rationale_chars: int = Field(default=MAX_TRANSIENT_TEXT_CHARS,gt=0,le=MAX_TRANSIENT_TEXT_CHARS)
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
    # Runtime-issued, value-free field hypotheses and bounded overlap counts.
    # Their candidate status is preserved; neither is semantic evidence.
    schema_knowledge: dict = Field(default_factory=dict)
    relation_candidates: dict = Field(default_factory=dict)
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
    # Original conflicts stay visible even if a later repair drops the field.
    # Keep the normal two rounds of six candidates bounded across later tasks.
    hierarchy_identity_requests: list[ProposalRepair] = Field(default_factory=list,
        max_length=MAX_HIERARCHY_IDENTITY_REQUESTS)
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
