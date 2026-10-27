"""Compiler feedback repairs contracts without discarding good siblings or evidence guards."""
import json
import pytest

from lightgraph.contracts import ExtractionGoal, RuleSpec
from lightgraph.discovery import discover, discover_references
from lightgraph.sources import MemorySource
from lightgraph.tests.test_agent_pipeline import client, input_packet, response, group, table


def custom_property(name='status',kind='ReportItem'):
    return {'name':name,'description':'A status attribute of the business object.',
            'value_type':'string','entity_kinds':[kind],'multi_valued':False}


def two_groups(packet, *, excluded=False):
    first=group(packet,'a','b')['proposals'][0]
    second=group(packet,'c','d',extra=[('e',{'role':'semantic_attribute','property_name':'status'})])['proposals'][0]
    if excluded:
        ids={c['name']:c['id'] for c in packet['metadata_digest']['columns']}
        second['claim']['members'].append({'column_id':ids['amount'],'role':'fact_value','exclude_from_projection':True})
    second['rationale']='MODEL_VALUE_MUST_NOT_ENTER_REPAIR_837'
    return {'task':packet['task'],'proposals':[first,second]}


def test_contract_failure_repairs_only_rejected_group_and_retains_valid_sibling(monkeypatch):
    t=table('objects',['a','b','c','d','e'])
    packets=[]
    def handler(request):
        _,p=input_packet(request);packets.append(p)
        if p['task']=='T0_discovery':return response(two_groups(p))
        assert p['task']=='T5_revision'
        assert len(p['proposal_repairs'])==1
        assert p['proposal_repairs'][0]['code']=='unregistered_attribute_semantics'
        assert 'MODEL_VALUE_MUST_NOT_ENTER_REPAIR_837' not in json.dumps(p)
        fixed=group(p,'c','d',extra=[('e',{'role':'semantic_attribute','property_name':'status'})])
        fixed['semantic_schema']={'properties':[custom_property()]}
        return response(fixed)
    with MemorySource([t],{'public.objects':[('A','Alpha','C','Charlie','active')]}) as source:
        result=discover(source,model=client(monkeypatch,handler))
    assert {tuple(rule.identity.key_columns) for rule in result.rules}=={('a',),('c',)}
    assert not result.issues
    assert result.stats['compile_repair_rounds']==result.stats['recovered_proposals']==1
    assert len(packets)==2


def test_unchanged_contract_failure_is_bounded_and_keeps_usable_sibling(monkeypatch):
    t=table('objects',['a','b','c','d','e'])
    packets=[]
    def handler(request):
        _,p=input_packet(request);packets.append(p)
        batch=two_groups(p)
        if p['task']=='T5_revision':batch['proposals']=batch['proposals'][1:]
        return response(batch)
    with MemorySource([t],{'public.objects':[('A','Alpha','C','Charlie','active')]}) as source:
        result=discover(source,model=client(monkeypatch,handler))
    assert [rule.identity.key_columns for rule in result.rules]==[['a']]
    assert len(packets)==2
    assert result.stats['deferred_proposals']==1
    assert any(i['code']=='unregistered_attribute_semantics' and i['status']=='deferred' for i in result.issues)


def test_repair_budget_exhaustion_preserves_independent_valid_rule(monkeypatch):
    from dataclasses import replace
    from lightgraph.discovery.budget import BudgetAllocator
    t=table('objects',['a','b','c','d','e'])
    model=client(monkeypatch,lambda request:response(two_groups(input_packet(request)[1])))
    model.budget=BudgetAllocator(replace(model.budget.limits,llm_calls=1))
    with MemorySource([t],{'public.objects':[('A','Alpha','C','Charlie','active')]}) as source:
        result=discover(source,model=model)
    assert [rule.identity.key_columns for rule in result.rules]==[['a']]
    assert model.usage['calls']==1
    assert any(i['code']=='llm_calls_budget_exhausted' and i['status']=='deferred' for i in result.issues)
    assert result.stats['deferred_proposals']==1
    assert result.budget['reserved']=={}


def test_semantic_refutation_does_not_enter_contract_repair(monkeypatch):
    t=table('objects',['a','b','c','d'])
    tasks=[]
    def handler(request):
        _,p=input_packet(request);tasks.append(p['task'])
        if p['task']=='semantic_review':
            return response({'proposition':p['semantic_proposition'],'decision':'refuted','basis':'data_inference',
                'evidence':[{'reference':p['metadata_digest']['columns'][0]['id'],'stance':'refutes'}]})
        assert p['task']=='T0_discovery'
        batch=group(p,'a','b')
        ids={c['name']:c['id'] for c in p['metadata_digest']['columns']}
        batch['proposals'].append({'kind':'wide_hierarchy','claim':{'group_id':'tree','levels':[
            {'position':1,'key':ids['c']},{'position':2,'key':ids['d']}]},'evidence_cited':[ids['c']]})
        return response(batch)
    with MemorySource([t],{'public.objects':[('A','Alpha','C','D')]}) as source:
        result=discover(source,model=client(monkeypatch,handler))
    assert [rule.identity.key_columns for rule in result.rules]==[['a']]
    assert tasks==['T0_discovery','semantic_review']
    assert any(i['code']=='semantic_claim_refuted' and i['status']=='unresolved' for i in result.issues)


def test_repair_cannot_reintroduce_original_fact_value_as_business_attribute(monkeypatch):
    t=table('objects',['a','b','c','d','e','amount'])
    def handler(request):
        _,p=input_packet(request)
        if p['task']=='T0_discovery':return response(two_groups(p,excluded=True))
        fixed=group(p,'c','d',extra=[('e',{'role':'semantic_attribute','property_name':'status'}),
                                    ('amount',{'role':'semantic_attribute','property_name':'description'})])
        fixed['semantic_schema']={'properties':[custom_property()]}
        return response(fixed)
    with MemorySource([t],{'public.objects':[('A','Alpha','C','Charlie','active',9999)]}) as source:
        result=discover(source,model=client(monkeypatch,handler))
    assert len(result.rules)==2
    for rule in result.rules:
        assert 'amount' not in rule.attributes.single_valued.values()
        assert 'amount' not in rule.attributes.multi_valued.values()
        assert 'amount' not in rule.identity.key_columns
        assert 'amount' in {item.column for item in rule.attributes.excluded}


@pytest.mark.parametrize('affects',[['identity'],['projection']])
def test_new_exclusion_removes_previously_compiled_sibling(monkeypatch,affects):
    t=table('objects',['a','b','c','d','e'])
    def handler(request):
        _,p=input_packet(request)
        if p['task']=='T0_discovery':return response(two_groups(p))
        fixed=group(p,'c','d',extra=[('e',{'role':'semantic_attribute','property_name':'status'})])
        fixed['semantic_schema']={'properties':[custom_property()]}
        key=next(c['id'] for c in p['metadata_digest']['columns'] if c['name']=='a')
        fixed['proposals'].append({'kind':'exclusion','claim':{'exclusions':[{
            'column_id':key,'reason':'out_of_goal','affects':affects}]},'evidence_cited':[key]})
        return response(fixed)
    with MemorySource([t],{'public.objects':[('A','Alpha','C','Charlie','active')]}) as source:
        result=discover(source,model=client(monkeypatch,handler))
    assert [rule.identity.key_columns for rule in result.rules]==[['c']]
    assert any(i['code']=='proposal_exclusion_conflict' for i in result.issues)


def test_contract_repair_cannot_erase_original_fd_counterexample(monkeypatch):
    t=table('objects',['code','label','custom'])
    prior=RuleSpec(rule_id='original',kind='column_group_entity',source={'source_id':'s','table':'objects'},
        identity={'key_columns':['code'],'entity_kind':'ReportItem'},attributes={'single_valued':{'label':'label'}})
    packets=[]
    def handler(request):
        _,p=input_packet(request);packets.append(p)
        proposal=group(p,'code','label',extra=[('custom',{'role':'semantic_attribute','property_name':'status'})])
        label=next(c['id'] for c in p['metadata_digest']['columns'] if c['name']=='label')
        proposal['proposals'][0]['claim']['members'].append({'column_id':label,'role':'identity_code','in_identity':True})
        if p.get('proposal_repair_round'):
            assert 'fd_violation' in p['allowed_revision_codes']
            proposal['semantic_schema']={'properties':[custom_property()]}
        return response(proposal)
    with MemorySource([t],{'public.objects':[('K','First','x'),('K','Second','x')]}) as source:
        result=discover(source,model=client(monkeypatch,handler),revision_rules=[prior],
                        iteration_context={'codes':['fd_violation']})
    assert len(packets)==2
    assert not result.rules
    assert any(i['code']=='revision_masks_counterexample' for i in result.issues)


def test_same_rule_id_conflicting_repair_does_not_choose_first_interpretation(monkeypatch):
    t=table('objects',['a','b','c','d','e'])
    def handler(request):
        _,p=input_packet(request)
        if p['task']=='T0_discovery':return response(two_groups(p))
        return response(group(p,'a','d'))
    with MemorySource([t],{'public.objects':[('A','Alpha','C','Charlie','active')]}) as source:
        result=discover(source,model=client(monkeypatch,handler))
    assert not result.rules
    assert any(i['code']=='conflicting_rule_proposals' for i in result.issues)


@pytest.mark.parametrize('initial_error',['reference_hierarchy_level_required','relation_endpoint_kind_mismatch'])
def test_hierarchy_endpoint_contract_is_repaired_with_registered_binding(monkeypatch,initial_error):
    t=table('objects',['parent','parent_name','child','child_name','center','center_name'])
    hierarchy=RuleSpec(rule_id='tree',kind='wide_hierarchy',source={'source_id':'s','table':'objects'},
        identity={'key_columns':['parent'],'entity_kind':'ReportItem','level_policy':'level_qualified'},
        hierarchy={'levels':[{'level':1,'code':'parent','label':'parent_name'},
                             {'level':2,'code':'child','label':'child_name'}],
                   'order_evidence':['user:hierarchy']},status='validated',validation_ref='v_tree')
    center=RuleSpec(rule_id='centers',kind='column_group_entity',source={'source_id':'s','table':'objects'},
        identity={'key_columns':['center'],'entity_kind':'Dimension'},status='validated',validation_ref='v_center')
    packets=[]
    def handler(request):
        _,p=input_packet(request);packets.append(p)
        if p['task']=='semantic_review':
            return response({'proposition':p['semantic_proposition'],'decision':'supported','basis':'data_inference',
                'evidence':[{'reference':'backend_endpoint_verification','stance':'supports'}]})
        assert p['task']=='T2_cross_table'
        assert [(b['level'],b['key_columns']) for b in p['entity_bindings'] if b['rule_id']=='tree']==[
            (1,['parent']),(2,['child'])]
        ids={c['name']:c['id'] for c in p['metadata_digest']['columns']}
        claim={'source_entity':'tree','target_entity':'centers','source_columns':[ids['child']],
               'target_columns':[ids['center']],'relation_kind':'assigned_to'}
        if initial_error=='relation_endpoint_kind_mismatch':
            claim.update(source_level=2,relation_kind='verified_reference')
        if p.get('proposal_repair_round'):
            assert p['proposal_repairs'][0]['code']==initial_error
            claim.update(source_level=2,relation_kind='assigned_to')
        return response({'task':p['task'],'semantic_schema':{'relations':[{'name':'assigned_to',
            'description':'The responsibility center assigned to this object.',
            'source_kinds':['ReportItem'],'target_kinds':['Dimension']}]},
            'proposals':[{'kind':'reference','claim':claim,'evidence_cited':[ids['child']]}]})
    with MemorySource([t],{'public.objects':[('A','Parent','B','Child','Z','Center')]}) as source:
        result=discover_references(source,[hierarchy,center],model=client(monkeypatch,handler))
    assert len(result.rules)==1,result.issues
    assert result.rules[0].relation.source_level==2
    assert result.rules[0].relation.target_level is None
    assert result.stats['compile_repair_rounds']==result.stats['recovered_proposals']==1
    assert not result.issues
    assert [p['task'] for p in packets]==['T2_cross_table','T2_cross_table','semantic_review']
