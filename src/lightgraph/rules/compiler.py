"""Compile the declarative rule contract to a closed operator plan.

Operators never interpret code or SQL. An explicit plan must agree with the
canonical contract: a supplied argument cannot silently override scope/columns.
"""
from ..contracts import OperatorSpec, canonical
from ..errors import LightGraphError
from ..store.policy import PersistencePolicy


def canonical_plan(rule):
    from .engine import RuleEngine
    columns=RuleEngine.columns(None,rule)
    plan=[OperatorSpec(op='distinct_projection',args={'columns':columns})]
    if rule.concept_mapping:
        plan.append(OperatorSpec(op='project_concepts',args=rule.concept_mapping.model_dump()))
    if rule.kind=='wide_hierarchy':
        plan.extend([OperatorSpec(op='unpivot_levels',args={'levels':[l.model_dump() for l in rule.hierarchy.levels]}),
            OperatorSpec(op='compress_paths',args={'gap_policy':'split','fill_semantics_confirmed':rule.hierarchy.fill_semantics_confirmed})])
    if rule.kind!='reference':plan.append(OperatorSpec(op='emit_entity',args={'kind':rule.identity.entity_kind}))
    if rule.attributes.single_valued or rule.attributes.multi_valued:
        plan.append(OperatorSpec(op='project_attributes',args={'single_valued':rule.attributes.single_valued,'multi_valued':rule.attributes.multi_valued}))
    if rule.kind in ('reference','wide_hierarchy','long_hierarchy'):
        relation=rule.relation.relation if rule.relation else 'parent_of'
        plan.append(OperatorSpec(op='emit_relation',args={'relation':relation}))
    plan.append(OperatorSpec(op='reconcile_supports',args={}))
    return plan


def validate_plan(rule):
    PersistencePolicy().check_record(rule)
    if rule.operators:
        expected=canonical_plan(rule)
        if canonical(rule.operators)!=canonical(expected):
            raise LightGraphError('operator_plan_mismatch')
    return canonical_plan(rule)
