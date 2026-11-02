"""An operator or an evidence locator cannot silently create a second business graph."""
from copy import deepcopy

import pytest

from lightgraph.contracts import RuleSpec, canonical
from lightgraph.discovery.operator_conflicts import context_conflict_candidate
from lightgraph.rules.context_identity import competing_context_rules
from lightgraph.store import GraphStore
from lightgraph.tests.test_concept_mapping import coded_row, coded_rule, execute
from lightgraph.tests.test_rules_engine import table


def entity_rule(context):
    return RuleSpec(rule_id='plain_context', kind='column_group_entity', source=context.source,
        identity=context.identity, scope=context.scope, semantic_schema=context.semantic_schema)


@pytest.mark.parametrize('reverse',[False,True])
def test_cross_operator_same_source_identity_requires_review(reverse):
    concept=coded_rule();plain=entity_rule(concept)
    new,old=(plain,concept) if reverse else (concept,plain)
    assert competing_context_rules(new,[old])==[old]
    revised=new.model_copy(deep=True);revised.rule_id=old.rule_id;revised.revision=2
    assert not competing_context_rules(revised,[old])  # Explicit full replacement.
    raw=coded_row();candidate=context_conflict_candidate(new,[old],table=table(list(raw)))
    assert candidate.failure_code=='conflicting_rule_proposals'
    assert set(candidate.rule_refs)=={new.rule_id,old.rule_id}
    assert len(candidate.mappings)==2
    payload=canonical(candidate.model_dump())
    assert all(value not in payload for value in ['占用','梨','批发','M01','C01'])
    assert all(c.startswith('c_') for c in candidate.column_refs)


@pytest.mark.parametrize('change',['source','kind','key','namespace'])
def test_unrelated_identity_is_not_a_competing_operator(change):
    concept=coded_rule();plain=entity_rule(concept).model_copy(deep=True)
    if change=='source':plain.source.table='another_table'
    if change=='kind':plain.identity.entity_kind='DifferentContext'
    if change=='key':plain.identity.key_columns=['subject','other_key']
    if change=='namespace':plain.scope.namespaces=['other_scope']
    assert not competing_context_rules(concept,[plain])


def test_only_renaming_relations_needs_adjudication_not_another_graph():
    first=coded_rule();second=deepcopy(first);second.rule_id='other'
    second.concept_mapping.components[0].relation='applies_to_metric'
    assert competing_context_rules(second,[first])==[first]
    second.concept_mapping.components[0].relation=first.concept_mapping.components[0].relation
    second.concept_mapping.evidence_ref='decl@1:another_section'
    assert competing_context_rules(second,[first])==[first]
    second.concept_mapping.components[0].definition_ref='different-business-definition'
    assert not competing_context_rules(second,[first])


@pytest.mark.parametrize('change',['evidence','order','label_column','key_order'])
def test_same_assignment_under_a_new_projection_spec_requires_revision(change):
    first=coded_rule();second=deepcopy(first);second.rule_id='other'
    if change=='evidence':second.concept_mapping.components[0].scope_evidence_ref='decl@1:other'
    if change=='order':second.concept_mapping.components.reverse()
    if change=='label_column':second.concept_mapping.components[0].column='another_name'
    if change=='key_order':second.identity.key_columns.reverse()
    assert competing_context_rules(second,[first])==[first]


def test_unchanged_projection_can_add_support_without_migrating_old_bindings():
    first=coded_rule();second=deepcopy(first);second.rule_id='other'
    assert not competing_context_rules(second,[first])
    with GraphStore() as store:
        assert execute(store,[coded_row()],first).published
        old=store.view({'A'})
        assert execute(store,[coded_row()],second).published
        graph=store.view({'A'})
        assert {n['id'] for n in graph['nodes']}=={n['id'] for n in old['nodes']}
        assert {e['id'] for e in graph['edges']}=={e['id'] for e in old['edges']}
        assert {s['rule_id'] for s in graph['supports']}=={first.rule_id,second.rule_id}
