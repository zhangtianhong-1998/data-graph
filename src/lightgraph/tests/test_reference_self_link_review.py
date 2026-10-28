"""Reflexive custom relations need an exact reviewed business permission."""
import copy

import pytest

from lightgraph.contracts import RuleSpec
from lightgraph.discovery.workflow import discover_references_with_agent, _reference_relation_contract
from lightgraph.rules import RuleEngine
from lightgraph.semantic_schema import SemanticSchema
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import client, input_packet, response, table


OMITTED=object()


def inputs():
    source=table('objects',['code','label','target','other'])
    rules=[RuleSpec(rule_id='objects',kind='column_group_entity',source={'source_id':'s','table':'objects'},
        identity={'key_columns':['code'],'entity_kind':'ReportItem'},
        attributes={'single_valued':{'label':'label'}}),
        RuleSpec(rule_id='other',kind='column_group_entity',source={'source_id':'s','table':'objects'},
        identity={'key_columns':['other'],'entity_kind':'Dimension'})]
    return source,rules


def relation(policy):
    value={'name':'can_compare_to','description':'The business object can be compared to the selected object.',
        'source_kinds':['ReportItem'],'target_kinds':['ReportItem']}
    if policy is not OMITTED:value['allow_self_links']=policy
    return value


def handler_factory(policy,decision,packets,*,condition=None,rewrite_policy=False):
    def handler(request):
        _,packet=input_packet(request);packets.append(packet)
        if packet['task']=='semantic_review':
            proposition=packet['semantic_proposition']
            assert proposition['relation_definition']=={**relation(policy),'allow_self_links':policy is True}
            assert 'reference_self_link_policy' in packet['review_stage_contract']['programmatic_checks']['checks']
            assert 'Do not authorize them solely because endpoint values match' in packet['subtask']
            value={'proposition_ref':packet['semantic_proposition_ref'],'decision':decision,
                'basis':'data_inference','evidence':[{'reference':'backend_endpoint_verification',
                    'stance':'refutes' if decision=='refuted' else 'supports'}]}
            if condition:value['conditions']=[{'state':condition}]
            if rewrite_policy:
                value['proposition']=copy.deepcopy(proposition)
                value['proposition']['relation_definition']['allow_self_links']=False
            return response(value)
        assert packet['task']=='T2_cross_table'
        ids={item['name']:item['id'] for item in packet['metadata_digest']['columns']}
        return response({'task':packet['task'],'semantic_schema':{'relations':[relation(policy)]},
            'proposals':[{'kind':'reference','claim':{'source_entity':'objects','target_entity':'objects',
                'source_columns':[ids['code']],'target_columns':[ids['target']],
                'relation_kind':'can_compare_to'},'evidence_cited':[ids['code']]}]})
    return handler


@pytest.mark.parametrize('policy',[OMITTED,False,True])
def test_only_exact_reviewed_explicit_permission_can_publish_self_link(monkeypatch,policy):
    source_table,base=inputs();packets=[]
    model=client(monkeypatch,handler_factory(policy,'supported',packets))
    with MemorySource([source_table],{'public.objects':[('A','Alpha','A','D')]}) as source,GraphStore() as store:
        engine=RuleEngine(store)
        for rule in base:assert engine.execute(source,rule).published
        result=discover_references_with_agent(source,store.get_rules(),model=model)
        assert len(result.rules)==1 and not result.issues
        candidate=result.rules[0]
        assert ('semantic_review:allow_self_links' in candidate.evidence.semantic_basis)==(policy is True)
        assert candidate.evidence.semantic_decision=='model_review'
        outcome=engine.execute(source,candidate)
        assert outcome.published is (policy is True)
        if policy is not True:assert 'reference_self_link_forbidden' in outcome.codes
        edges=store.view({'public'})['edges']
        assert len(edges)==int(policy is True)
        if edges:assert edges[0]['source']==edges[0]['target']
    assert [p['task'] for p in packets]==['T2_cross_table','semantic_review']


@pytest.mark.parametrize('decision,condition,code',[
    ('refuted',None,'semantic_claim_refuted'),('unresolved',None,'semantic_claim_unresolved'),
    ('supported','unknown','semantic_condition_unresolved'),
    ('supported','unsatisfied','semantic_condition_unresolved')])
def test_declaring_permission_cannot_override_negative_or_conditional_review(monkeypatch,decision,condition,code):
    source_table,base=inputs();packets=[]
    model=client(monkeypatch,handler_factory(True,decision,packets,condition=condition))
    with MemorySource([source_table],{'public.objects':[('A','Alpha','A','D')]}) as source,GraphStore() as store:
        engine=RuleEngine(store)
        for rule in base:assert engine.execute(source,rule).published
        result=discover_references_with_agent(source,store.get_rules(),model=model)
        assert not result.rules and any(issue['code']==code for issue in result.issues)
        assert not store.view({'public'})['edges']
    assert [p['task'] for p in packets]==['T2_cross_table','semantic_review']


def test_legacy_echo_cannot_review_a_different_reflexive_policy(monkeypatch):
    source_table,base=inputs();packets=[]
    model=client(monkeypatch,handler_factory(True,'supported',packets,rewrite_policy=True))
    with MemorySource([source_table],{'public.objects':[('A','Alpha','A','D')]}) as source,GraphStore() as store:
        engine=RuleEngine(store)
        for rule in base:assert engine.execute(source,rule).published
        result=discover_references_with_agent(source,store.get_rules(),model=model)
        assert not result.rules
        assert any(issue['code']=='semantic_review_proposition_mismatch' for issue in result.issues)
        assert not store.view({'public'})['edges']
    assert [p['task'] for p in packets]==['T2_cross_table','semantic_review','semantic_review']


def test_builtin_relation_review_uses_actual_closed_contract():
    contract=_reference_relation_contract(SemanticSchema(),'parent_of')
    assert contract['definition_origin']=='builtin' and contract['allow_self_links'] is False
    assert 'ReportItem' in contract['source_kinds'] and 'ReportItem' in contract['target_kinds']
