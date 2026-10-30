"""Defer invalid contracts without hiding failures or discarding valid siblings."""
import json

import pytest
from pydantic import ValidationError

from lightgraph.contracts import ExtractionGoal, HierarchySpec
from lightgraph.discovery import discover
from lightgraph.discovery.engine import _column_id, _compile_model
from lightgraph.discovery.models import ProposalBatch, DiscoveryResult
from lightgraph.discovery.workflow import _compile
from lightgraph.sources import MemorySource
from lightgraph.rules import RuleEngine
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import client, group, input_packet, response, table


CANARY='UNRETAINED_CONTEXT_VALUE_2365'


def hierarchy(packet, qualifiers):
    ids={c['name']:c['id'] for c in packet['metadata_digest']['columns']}
    return {'kind':'wide_hierarchy','claim':{'group_id':'tree','entity_kind_hint':'ReportItem',
        'levels':[{'position':1,'key':ids['c']},{'position':2,'key':ids['d']}],
        'path_semantics':'relative_path','identity_domain':'report_items','taxonomy':'report_path',
        'position_semantics':'ordinal_slots','context_qualifiers':qualifiers,
        'ordering_evidence':[ids['c']]},'evidence_cited':[ids['c']]}


def batch(t, qualifiers):
    packet={'metadata_digest':{'columns':[{'name':c.name,'id':_column_id(t,c.name)} for c in t.columns]}}
    return ProposalBatch.model_validate({'task':'T0_discovery','proposals':[hierarchy(packet,qualifiers)]})


@pytest.mark.parametrize('qualifiers,code',[({'source_subject':CANARY},'invalid_hierarchy_context'),
                                         ({'condition':''},'empty_hierarchy_context')])
def test_exploratory_context_mismatch_is_reproduced_at_executable_contract(qualifiers,code):
    t=table('objects',['a','b','c','d'])
    proposal=batch(t,qualifiers)  # Valid LLM proposal, invalid executable contract.
    with pytest.raises(ValidationError) as caught:
        _compile_model(proposal,t,ExtractionGoal(),[],[],semantic_evidence=['review:approved'])
    assert caught.value.title=='HierarchySpec'
    assert caught.value.errors(include_input=False)[0]['ctx']['error'].args==(code,)
    result=DiscoveryResult();failures=[]
    assert _compile(proposal,t,ExtractionGoal(),[],result,failures=failures)==[]
    assert [failure_code for _,failure_code in failures]==[code]
    assert not result.issues



@pytest.mark.parametrize('qualifiers,code',[({'source_subject':CANARY},'invalid_hierarchy_context'),
                                         ({'condition':''},'empty_hierarchy_context')])
def test_invalid_hierarchy_context_is_deferred_without_review_and_good_sibling_publishes(monkeypatch,qualifiers,code):
    t=table('objects',['a','b','c','d']);tasks=[]
    def handler(request):
        _,packet=input_packet(request);tasks.append(packet['task'])
        assert packet['task']=='T0_discovery'
        value=group(packet,'a','b');value['proposals'].append(hierarchy(packet,qualifiers))
        return response(value)
    with MemorySource([t],{'public.objects':[('A','Alpha','P','C')]}) as source,GraphStore() as store:
        result=discover(source,model=client(monkeypatch,handler))
        assert [rule.kind for rule in result.rules]==['column_group_entity']
        assert RuleEngine(store).execute(source,result.rules[0]).published
        graph=store.view({'public'})
        assert len(graph['nodes'])==1 and graph['nodes'][0]['label']=='Alpha'
        assert not graph['edges']
    assert tasks==['T0_discovery']
    assert any(issue['code']==code and issue['status']=='deferred' for issue in result.issues)
    assert CANARY not in json.dumps(result.model_dump())


@pytest.mark.parametrize('validation_error',[False,True])
def test_unexpected_compiler_failures_are_not_reclassified(monkeypatch,validation_error):
    t=table('objects',['a','b','c','d'])
    def broken(*args,**kwargs):
        if validation_error:
            HierarchySpec(position_semantics='unexpected_schema_mismatch')
        raise RuntimeError('compiler_programming_failure')
    monkeypatch.setattr('lightgraph.discovery.engine._compile_model',broken)
    with pytest.raises(ValidationError if validation_error else RuntimeError):
        _compile(batch(t,{}),t,ExtractionGoal(),[],DiscoveryResult(),failures=[])



@pytest.mark.parametrize('missing',[('identity_domain',),('taxonomy',),('position_semantics',),
                                    ('identity_domain','taxonomy','position_semantics')])
def test_missing_relative_semantics_stay_deferred_without_paid_review(monkeypatch,missing):
    t=table('objects',['a','b','c','d']);tasks=[]
    def handler(request):
        _,packet=input_packet(request);tasks.append(packet['task'])
        assert packet['task']=='T0_discovery'
        proposal=hierarchy(packet,{})
        for field in missing:proposal['claim'].pop(field)
        value=group(packet,'a','b');value['proposals'].append(proposal)
        return response(value)
    with MemorySource([t],{'public.objects':[('A','Alpha','P','C')]}) as source:
        result=discover(source,model=client(monkeypatch,handler))
    assert tasks==['T0_discovery']
    assert [rule.kind for rule in result.rules]==['column_group_entity']
    assert any(issue['code']=='hierarchy_position_semantics_missing' and issue['status']=='deferred'
        for issue in result.issues)


@pytest.mark.parametrize('decision',['supported','refuted','unresolved'])
def test_complete_relative_claim_still_requires_semantic_review(monkeypatch,decision):
    t=table('objects',['a','b','c','d']);tasks=[]
    def handler(request):
        _,packet=input_packet(request);tasks.append(packet['task'])
        if packet['task']=='semantic_review':
            return response({'proposition_ref':packet['semantic_proposition_ref'],
                'decision':decision,'basis':'data_inference','evidence':[{
                    'reference':packet['metadata_digest']['columns'][2]['id'],
                    'stance':'supports' if decision=='supported' else 'refutes' if decision=='refuted' else 'context'}]})
        if packet['task']=='T5_revision':return response({'task':'T5_revision','abstain':True})
        assert packet['task']=='T0_discovery'
        value=group(packet,'a','b');value['proposals'].append(hierarchy(packet,{}))
        return response(value)
    with MemorySource([t],{'public.objects':[('A','Alpha','P','C')]}) as source,GraphStore() as store:
        result=discover(source,model=client(monkeypatch,handler))
        for rule in result.rules:assert RuleEngine(store).execute(source,rule).published
        graph=store.view({'public'})
        assert len(graph['nodes'])==(3 if decision=='supported' else 1)
        assert len(graph['edges'])==(1 if decision=='supported' else 0)
    assert tasks==['T0_discovery','semantic_review']+([] if decision=='supported' else ['T5_revision'])
    if decision=='supported':assert not result.issues
    else:assert any(issue['code']=='semantic_claim_'+decision for issue in result.issues)


def test_legacy_hierarchy_does_not_require_relative_semantics(monkeypatch):
    t=table('objects',['a','b','c','d']);tasks=[]
    def handler(request):
        _,packet=input_packet(request);tasks.append(packet['task'])
        if packet['task']=='semantic_review':
            assert packet['semantic_proposition']['claim']['path_semantics']=='legacy'
            return response({'proposition_ref':packet['semantic_proposition_ref'],
                'decision':'supported','basis':'data_inference','evidence':[{
                    'reference':packet['metadata_digest']['columns'][0]['id'],'stance':'supports'}]})
        proposal=hierarchy(packet,{})
        proposal['claim']['path_semantics']='legacy'
        for field in ('identity_domain','taxonomy','position_semantics'):proposal['claim'].pop(field)
        return response({'task':packet['task'],'proposals':[proposal]})
    with MemorySource([t],{'public.objects':[('A','Alpha','P','C')]}) as source:
        result=discover(source,model=client(monkeypatch,handler))
    assert [rule.kind for rule in result.rules]==['wide_hierarchy']
    assert tasks==['T0_discovery','semantic_review'] and not result.issues
