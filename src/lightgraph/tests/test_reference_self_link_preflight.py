"""Only provably identical endpoint mappings can skip paid semantic review."""
import pytest

from lightgraph.discovery.workflow import discover_references_with_agent
from lightgraph.rules import RuleEngine
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import client, input_packet, response, table
from lightgraph.tests.test_hierarchy_endpoints import rules as hierarchy_rules
from lightgraph.tests.test_reference_self_link_review import inputs, relation, OMITTED


def supported(packet):
    return response({'proposition_ref': packet['semantic_proposition_ref'], 'decision': 'supported',
        'basis': 'data_inference', 'evidence': [
            {'reference': 'backend_endpoint_verification', 'stance': 'supports'}]})


@pytest.mark.parametrize('policy', [OMITTED, False, True])
@pytest.mark.parametrize('builtin', [False, True])
def test_identical_mapping_preflight_keeps_explicit_reflexive_permission(monkeypatch, policy, builtin):
    source_table, base = inputs()
    tasks = []
    def handler(request):
        _, packet = input_packet(request)
        tasks.append(packet['task'])
        if packet['task'] == 'semantic_review':
            assert not builtin and policy is True
            return supported(packet)
        ids = {c['name']: c['id'] for c in packet['metadata_digest']['columns']}
        return response({'task': packet['task'],
            'semantic_schema': {} if builtin else {'relations': [relation(policy)]},
            'proposals': [{'kind': 'reference', 'claim': {
                'source_entity': 'objects', 'target_entity': 'objects',
                'source_columns': [ids['code']], 'target_columns': [ids['code']],
                'relation_kind': 'parent_of' if builtin else 'can_compare_to'},
                'evidence_cited': [ids['code']]}]})
    allowed = not builtin and policy is True
    with MemorySource([source_table], {'public.objects': [('A', 'Alpha', 'A', 'D')]}) as source, GraphStore() as store:
        engine = RuleEngine(store)
        for rule in base:
            assert engine.execute(source, rule).published
        queries = source.stats.queries
        result = discover_references_with_agent(source, store.get_rules(), model=client(monkeypatch, handler))
        if allowed:
            assert len(result.rules) == 1 and not result.issues
            assert 'semantic_review:allow_self_links' in result.rules[0].evidence.semantic_basis
            assert engine.execute(source, result.rules[0]).published
        else:
            assert result.rules == []
            assert [i['code'] for i in result.issues] == ['reference_self_link_forbidden']
            assert source.stats.queries == queries
    assert tasks == (['T2_cross_table', 'semantic_review'] if allowed else ['T2_cross_table'])


def test_forbidden_same_mapping_does_not_drop_independent_relation(monkeypatch):
    source_table, base = inputs()
    tasks = []
    def handler(request):
        _, packet = input_packet(request)
        tasks.append(packet['task'])
        if packet['task'] == 'semantic_review':
            assert packet['semantic_proposition']['claim']['target_entity'] == 'other'
            return supported(packet)
        ids = {c['name']: c['id'] for c in packet['metadata_digest']['columns']}
        proposals = [{'kind': 'reference', 'claim': {
            'source_entity': 'objects', 'target_entity': target,
            'source_columns': [ids['code']], 'target_columns': [ids[column]],
            'relation_kind': name}, 'evidence_cited': [ids['code']]}
            for target, column, name in [('objects', 'code', 'parent_of'), ('other', 'other', 'assigned_to')]]
        return response({'task': packet['task'], 'semantic_schema': {'relations': [{
            'name': 'assigned_to', 'description': 'Object assigned to dimension.',
            'source_kinds': ['ReportItem'], 'target_kinds': ['Dimension']}]}, 'proposals': proposals})
    with MemorySource([source_table], {'public.objects': [('A', 'Alpha', 'A', 'D')]}) as source, GraphStore() as store:
        engine = RuleEngine(store)
        for rule in base:
            assert engine.execute(source, rule).published
        result = discover_references_with_agent(source, store.get_rules(), model=client(monkeypatch, handler))
        assert len(result.rules) == 1 and result.rules[0].relation.relation == 'assigned_to'
        assert [i['code'] for i in result.issues] == ['reference_self_link_forbidden']
        assert engine.execute(source, result.rules[0]).published
    assert tasks == ['T2_cross_table', 'semantic_review']


def test_same_hierarchy_mapping_at_different_levels_is_not_assumed_reflexive(monkeypatch):
    hierarchy, _, _ = hierarchy_rules()
    source_table = table('paths', ['tenant', 'first_code', 'first_name', 'second_code', 'second_name', 'center'])
    tasks = []
    def handler(request):
        _, packet = input_packet(request)
        tasks.append(packet['task'])
        if packet['task'] == 'semantic_review':
            return supported(packet)
        ids = {c['name']: c['id'] for c in packet['metadata_digest']['columns']}
        mapping = [ids['tenant'], ids['first_code']]
        return response({'task': packet['task'], 'semantic_schema': {'relations': [relation(False)]},
            'proposals': [{'kind': 'reference', 'claim': {
                'source_entity': 'hierarchy', 'target_entity': 'hierarchy',
                'source_level': 1, 'target_level': 2, 'source_columns': mapping,
                'target_columns': mapping, 'relation_kind': 'can_compare_to'},
                'evidence_cited': [ids['first_code']]}]})
    with MemorySource([source_table], {'public.paths': [('A', 'K', 'Parent', 'K', 'Child', 'Center')]}) as source, GraphStore() as store:
        engine = RuleEngine(store)
        assert engine.execute(source, hierarchy).published
        result = discover_references_with_agent(source, store.get_rules(), model=client(monkeypatch, handler))
        assert len(result.rules) == 1 and not result.issues
        assert engine.execute(source, result.rules[0]).published
        edges = [e for e in store.view({'A'})['edges'] if e['relation'] == 'can_compare_to']
        assert len(edges) == 1 and edges[0]['source'] != edges[0]['target']
    assert tasks == ['T2_cross_table', 'semantic_review']
