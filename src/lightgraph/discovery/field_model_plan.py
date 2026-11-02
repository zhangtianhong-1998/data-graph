"""Reuse remembered field mappings without inventing their hierarchy semantics."""
from __future__ import annotations

from typing import Literal
from pydantic import Field

from lightgraph.contracts import StrictModel
from lightgraph.errors import LightGraphError
from .hierarchy_plan import HierarchyPlan, compile_hierarchy_plan


class FieldModelHierarchyPlan(StrictModel):
    object_id: str = Field(description='Issued active object ID in schema_knowledge.objects. Its position and namespace columns are reused exactly.')
    plan_id: str = Field(min_length=1, max_length=128)
    entity_type: str = Field(min_length=1, max_length=64)
    identity_scope: Literal['source', 'namespace', 'global']
    shared_identity_domain: str | None = Field(default=None, max_length=256)
    position_meaning: Literal['relative_ordinal', 'relative_ordered', 'fixed_levels']
    allow_multiple_parents: bool = Field(strict=True)
    repeat_is_fill: bool = Field(strict=True)
    relation_type: Literal['parent_of']
    evidence: list[str] = Field(min_length=1, max_length=16)
    conditions: dict[str, str] = Field(default_factory=dict,
        json_schema_extra={'propertyNames': {'enum': ['condition', 'valid_period', 'period', 'hierarchy_version']}},
        description='Optional business qualifiers: condition, valid_period, period or hierarchy_version. '
            'Use an empty object when none apply; structural checks and review obligations are not qualifiers.')


def field_model_plan_tool_schema():
    return {'type':'function','function':{'name':'submit_field_model_hierarchy',
        'description':'Submit explicit hierarchy semantics for one retained field model. '
            'The program copies all positions, code/name/alias and namespace columns. '
            'Choose scope, shared domain, repeat meaning and multiple-parent permission from issued evidence. '
            'If field mappings need changes, revise the candidate object model first. '
            'This is a proposal requiring independent review and full execution, never approval.',
        'parameters':FieldModelHierarchyPlan.model_json_schema()}}


def compile_field_model_plan(raw, packet, *, validate_references=True):
    plan=FieldModelHierarchyPlan.model_validate(raw)
    matches=[obj for obj in packet.get('schema_knowledge',{}).get('objects',[])
        if obj.get('object_id')==plan.object_id and obj.get('status') not in
            {'superseded','conflict','published','local_validated'}]
    if len(matches)!=1:
        raise LightGraphError('field_model_reference_unavailable')
    obj=matches[0]
    positions=obj.get('positions') or []
    if len(positions)<2:
        raise LightGraphError('field_model_positions_required')
    body=plan.model_dump(exclude={'object_id'})
    body.update(positions=positions,namespace_columns=obj.get('namespace_columns') or [])
    return compile_hierarchy_plan(HierarchyPlan.model_validate(body),packet,
        validate_references=validate_references)
