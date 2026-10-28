"""New physical roles are bounded work references, never identity decisions."""
from dataclasses import asdict

import pytest

from lightgraph.contracts import ColumnSpec, RuleSpec, TableSpec, canonical
from lightgraph.identity.role_occurrences import detect_role_occurrences
from lightgraph.rules import RuleEngine
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore


def group(rule_id, column, *, table='people', namespace=False):
    return RuleSpec(rule_id=rule_id, kind='column_group_entity',
        source={'source_id': 's', 'table': table},
        scope={'namespaces': ['tenant'] if namespace else []},
        identity={'key_columns': [column, 'tenant'] if namespace else [column], 'entity_kind': 'ReportItem'})


def test_same_key_in_other_column_emits_safe_role_feedback_without_merging():
    rules = [group('person', 'person_code'), group('parent', 'parent_code')]
    table = TableSpec(source_id='s', name='people', columns=[ColumnSpec(name=name)
        for name in ['person_code', 'parent_code']])
    rows = {'public.people': [('CANARY_P1', 'CANARY_P2'), ('CANARY_P2', 'CANARY_P3'), ('CANARY_P3', 'CANARY_P1')]}
    with MemorySource([table], rows) as source, GraphStore() as store:
        engine = RuleEngine(store)
        for rule in rules:
            assert engine.execute(source, rule).published
        events, coverage = detect_role_occurrences(rules, engine.bindings)
        assert len(events) == 1 and events[0].count == 3
        assert events[0].rule_ids == ('parent', 'person')
        assert events[0].column_refs == ('s.public.people.parent_code', 's.public.people.person_code')
        assert events[0].code == 'unexplained_role_occurrence'
        assert 'CANARY' not in str([asdict(event) for event in events]) + str(coverage)
        assert len(store.view({'public'})['nodes']) == 6
        assert source.stats.queries == 2  # Detection consumes the validated index, not another query.


def test_namespace_values_and_existing_shared_entities_do_not_create_new_roles():
    left, right = group('left', 'person', namespace=True), group('right', 'parent', namespace=True)
    bindings = {('left', canonical(['P', 'private_A'])): 'a', ('right', canonical(['P', 'private_B'])): 'b'}
    assert detect_role_occurrences([left, right], bindings)[0] == []
    bindings[('right', canonical(['P', 'private_A']))] = 'a'
    assert detect_role_occurrences([left, right], bindings)[0] == []
    # Rules using the same physical columns are not new roles even if a later
    # definition revision resulted in a distinct draft identity.
    alias = group('alias', 'person', namespace=True)
    bindings[('alias', canonical(['P', 'private_A']))] = 'c'
    assert detect_role_occurrences([left, alias], bindings)[0] == []


def test_level_qualified_reappearance_is_visible_without_guessing_identity():
    hierarchy = RuleSpec(rule_id='levels', kind='wide_hierarchy',
        source={'source_id': 's', 'table': 'paths'},
        identity={'key_columns': ['l1'], 'entity_kind': 'ReportItem', 'level_policy': 'level_qualified'},
        hierarchy={'levels': [{'level': 1, 'code': 'l1'}, {'level': 2, 'code': 'l2'}],
                   'order_evidence': ['user:order']})
    bindings = {('levels', canonical([1, 'A'])): 'parent', ('levels', canonical([2, 'A'])): 'child'}
    events, _ = detect_role_occurrences([hierarchy], bindings)
    assert len(events) == 1 and events[0].rule_ids == ('levels',)
    assert events[0].column_refs == ('s.public.paths.l1', 's.public.paths.l2')


def test_cross_table_matches_enumerate_both_affected_tables():
    left, right = group('left', 'id', table='left'), group('right', 'code', table='right')
    events, _ = detect_role_occurrences([left, right], {
        ('left', canonical(['A'])): 'a', ('right', canonical(['A'])): 'b'})
    assert {event.source.table for event in events} == {'left', 'right'}
    assert all(event.rule_ids == ('left', 'right') and event.count == 1 for event in events)


def test_common_key_and_memory_are_bounded_before_pair_expansion():
    rules = [group(f'r{i}', f'c{i}') for i in range(100)]
    bindings = {(rule.rule_id, canonical(['A'])): f'entity_{i}' for i, rule in enumerate(rules)}
    events, coverage = detect_role_occurrences(rules, bindings, max_roles_per_key=2, max_events=1)
    assert len(events) == 1 and coverage['role_limit_omissions'] == 98
    limited, coverage = detect_role_occurrences(rules, bindings, max_bindings=2)
    assert len(limited) == 1 and coverage['binding_limit_omissions'] == 98
    assert detect_role_occurrences(rules, bindings, max_working_bytes=1)[1]['memory_limit_reached'] == 1


def inapplicable_rule(policy):
    if policy == 'reference':
        return RuleSpec(rule_id='resolved', kind='reference', source={'source_id': 's', 'table': 'paths'},
            identity={'key_columns': ['l1']}, relation={'source_rule_id': 'left', 'target_rule_id': 'right',
                'source_columns': ['l1'], 'target_columns': ['l2']})
    identity = {'key_columns': ['l1'], 'entity_kind': 'ReportItem', 'level_policy': policy}
    if policy == 'relative_path':
        identity.update(semantic_resolution='verified_identity_domain', identity_domain='items',
                        identity_evidence=['approved:identity'])
    return RuleSpec(rule_id='resolved', kind='wide_hierarchy', source={'source_id': 's', 'table': 'paths'},
        identity=identity, hierarchy={'levels': [{'level': 1, 'code': 'l1'}, {'level': 2, 'code': 'l2'}],
                                    'order_evidence': ['approved:order']})


@pytest.mark.parametrize('policy', ['relative_path', 'code_with_fill_merge', 'reference'])
@pytest.mark.parametrize('limits', [{'max_bindings': 1}, {'max_working_bytes': 1}])
def test_inapplicable_bindings_do_not_create_false_coverage_gaps(policy, limits):
    rule = inapplicable_rule(policy)
    bindings = {(rule.rule_id, canonical([code])): f'entity_{code}' for code in ['A', 'B']}
    events, coverage = detect_role_occurrences([rule], bindings, **limits)
    assert events == []
    assert all(value == 0 for value in coverage.values())


def test_inapplicable_bindings_cannot_starve_bounded_column_role_detection():
    resolved = inapplicable_rule('relative_path')
    rules = [group(f'r{i}', f'column_{i}') for i in range(3)]
    bindings = {(resolved.rule_id, canonical(['A'])): 'IGNORED_CANARY' * 100}
    bindings.update({(rule.rule_id, canonical(['A'])): f'entity_{i}' for i, rule in enumerate(rules)})
    events, coverage = detect_role_occurrences([resolved, *rules], bindings,
                                               max_bindings=2, max_working_bytes=512)
    assert len(events) == 1 and events[0].rule_ids == ('r0', 'r1')
    assert coverage['bindings_checked'] == 2
    assert coverage['binding_limit_omissions'] == 1  # The third applicable role is still bounded.
    assert coverage['memory_limit_reached'] == 0
    assert 'IGNORED_CANARY' not in str([asdict(event) for event in events]) + str(coverage)
