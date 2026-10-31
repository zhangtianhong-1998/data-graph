"""A small business plan translated into the existing reviewed hierarchy contract.

The Agent chooses identity sharing, positions, fill meaning and parent semantics.
This adapter only derives redundant executable fields; it grants no semantic
approval and never queries or publishes source data.
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from lightgraph.contracts import StrictModel, SourceRef, canonical
from lightgraph.errors import LightGraphError
from lightgraph.semantic_schema import EntityTypeDefinition, SemanticSchema
from lightgraph.store.policy import KINDS
from .models import HierarchyProposal, HypothesisLevel, ProposalBatch, WideHierarchy
from .reference_validation import validate_proposal_references


class PlanPosition(StrictModel):
    position: int = Field(ge=1, le=1024, description='Declared position in the path, not an entity identity.')
    code: str = Field(min_length=1, description='Issued column ID containing this position object code.')
    label: str | None = Field(default=None, description='Issued display-name column ID, if present.')
    alias: str | None = Field(default=None, description='Issued alternate-name column ID, if present.')
    aliases: list[str] = Field(default_factory=list,max_length=16,description=
        'All additional alternate-name or English-name column IDs for this same object position. '
        'Include known aliases in the first plan; these columns never enter identity.')


class HierarchyPlan(StrictModel):
    plan_id: str = Field(min_length=1, max_length=128)
    entity_type: str = Field(min_length=1, max_length=64)
    identity_scope: Literal['source', 'namespace', 'global'] = Field(description=
        'source isolates this physical table. namespace explicitly shares an identity domain across '
        'sources only within equal namespace values; global explicitly shares across namespace values too.')
    shared_identity_domain: str | None = Field(default=None, max_length=256, description=
        'Evidence-supported business identity domain, required only for namespace/global sharing.')
    namespace_columns: list[str] = Field(max_length=16, description='Issued column IDs separating identity namespaces.')
    positions: list[PlanPosition] = Field(min_length=2, max_length=16)
    position_meaning: Literal['relative_ordinal', 'relative_ordered', 'fixed_levels'] = Field(description=
        'relative_ordinal: codes identify objects independently of path position, missing numbered slots are gaps. '
        'relative_ordered: the same object reuse, but numbers only order available positions. '
        'fixed_levels: position is part of identity; only source-local identities are supported.')
    allow_multiple_parents: bool = Field(strict=True, description='Whether one object may have more than one parent.')
    repeat_is_fill: bool = Field(strict=True, description=
        'Whether consecutive occurrences of the same object mean repeated filling rather than a parent self-link.')
    relation_type: Literal['parent_of'] = Field(description=
        'This hierarchy operator implements parent-to-child edges. Other business predicates use reference proposals.')
    evidence: list[str] = Field(min_length=1, max_length=16, description='Issued evidence references supporting this exact plan.')
    conditions: dict[str, str] = Field(default_factory=dict, description=
        'Preserve applicable condition, valid_period, period or hierarchy_version qualifiers; never copy row values.')

    @model_validator(mode='after')
    def coherent_scope(self):
        if len({position.position for position in self.positions}) != len(self.positions):
            raise ValueError('hierarchy_plan_duplicate_position')
        if len(set(self.namespace_columns)) != len(self.namespace_columns):
            raise ValueError('hierarchy_plan_duplicate_namespace')
        if self.identity_scope == 'source':
            if self.shared_identity_domain is not None:
                raise ValueError('hierarchy_plan_unrequested_shared_domain')
        elif not self.shared_identity_domain or not self.shared_identity_domain.strip():
            raise ValueError('hierarchy_plan_shared_domain_required')
        if self.identity_scope == 'namespace' and not self.namespace_columns:
            raise ValueError('hierarchy_plan_namespace_required')
        if self.position_meaning == 'fixed_levels' and (
            self.identity_scope != 'source' or self.repeat_is_fill or self.allow_multiple_parents
        ):
            raise ValueError('hierarchy_plan_fixed_level_contract_unsupported')
        if set(self.conditions) - {'condition', 'valid_period', 'period', 'hierarchy_version'}:
            raise ValueError('hierarchy_plan_condition_unsupported')
        if any(not value.strip() for value in self.conditions.values()):
            raise ValueError('hierarchy_plan_empty_condition')
        if self.position_meaning == 'fixed_levels' and self.conditions:
            raise ValueError('hierarchy_plan_fixed_level_conditions_unsupported')
        return self


def hierarchy_plan_tool_schema() -> dict:
    """A submission tool; the runtime owns collecting its returned proposal."""
    return {'type': 'function', 'function': {
        'name': 'submit_hierarchy_plan',
        'description': 'Submit one evidence-supported hierarchy business plan. This creates a draft for '
            'independent semantic review and full data validation, never approval. Use issued column/evidence '
            'IDs and explicitly choose identity scope; do not infer cross-source sharing from equal codes.',
        'parameters': HierarchyPlan.model_json_schema(),
    }}


def compile_hierarchy_plan(plan: HierarchyPlan | dict, packet: dict, *, validate_references=True) -> ProposalBatch:
    """Derive internal fields without changing the Agent's identity decisions.

    Source-local relative identities use a metadata-only domain tied to the
    physical table and entity type, so position reuse does not imply cross-source
    merging. Shared domains require the Agent's explicit scope and domain name.
    Every result still enters the usual reference, goal and semantic checks.
    """
    plan = HierarchyPlan.model_validate(plan)
    if not packet.get('scope'):
        raise LightGraphError('hierarchy_plan_source_scope_missing')
    source = SourceRef.model_validate(packet['scope'])
    relative = plan.position_meaning != 'fixed_levels'
    domain = ('plan_source:' + canonical([source.source_id, source.schema_name, source.table, plan.entity_type])
              if plan.identity_scope == 'source' else
              'plan_shared:' + canonical([plan.shared_identity_domain, plan.entity_type]))
    refs = list(plan.evidence)
    claim = WideHierarchy(
        group_id=plan.plan_id, entity_kind_hint=plan.entity_type,
        levels=[HypothesisLevel(position=position.position, key=position.code,
            label=position.label, alias=position.alias,aliases=list(dict.fromkeys(position.aliases))) for position in plan.positions],
        namespace_columns=plan.namespace_columns,
        path_semantics='relative_path' if relative else 'legacy',
        identity_resolution='verified_identity_domain' if relative else 'source_scoped',
        identity_domain=domain if relative else '',
        identity_scope='domain_global' if plan.identity_scope == 'global' else 'namespace_local',
        taxonomy='plan_taxonomy:' + canonical([domain, plan.plan_id, plan.relation_type]) if relative else '',
        position_semantics={'relative_ordinal': 'ordinal_slots', 'relative_ordered': 'ordered_slots'}.get(plan.position_meaning),
        allow_dag=plan.allow_multiple_parents,
        repeat_policy='collapse_consecutive_equal_identity' if plan.repeat_is_fill else 'none',
        context_qualifiers=plan.conditions,
        ordering_evidence=refs, identity_evidence=refs if relative else [],
        attribute_evidence=refs if relative else [], taxonomy_evidence=refs if relative else [],
        position_evidence=refs if relative else [],
        global_scope_evidence=refs if plan.identity_scope == 'global' else [],
        context_evidence=refs if plan.conditions else [], dag_evidence=refs if plan.allow_multiple_parents else [],
    )
    schema = SemanticSchema(entity_types=[] if plan.entity_type in KINDS['business'] else [
        EntityTypeDefinition(name=plan.entity_type, description='Agent-proposed hierarchy business object type.')])
    batch = ProposalBatch(task=packet['task'], semantic_schema=schema, proposals=[
        HierarchyProposal(kind='wide_hierarchy', claim=claim, evidence_cited=refs)])
    if validate_references:validate_proposal_references(batch, packet)
    return batch
