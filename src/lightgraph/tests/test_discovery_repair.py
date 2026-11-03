"""Bad candidates remain explicit while valid siblings publish without repair Agents."""
import json
import pytest

from lightgraph.contracts import RuleSpec
from lightgraph.discovery import discover, discover_references
from lightgraph.sources import MemorySource
from lightgraph.tests.test_agent_pipeline import client, input_packet, response, group, table


def custom_property(name='status',kind='ReportItem'):
    return {'name':name,'description':'A status attribute of the business object.',
            'value_type':'string','entity_kinds':[kind],'multi_valued':False}


def two_groups(packet):
    first=group(packet,'a','b')['proposals'][0]
    second=group(packet,'c','d',extra=[('e',{'role':'semantic_attribute','property_name':'status'})])['proposals'][0]
    return {'task':packet['task'],'proposals':[first,second]}


def test_contract_failure_keeps_valid_sibling_without_another_agent(monkeypatch):
    t=table('objects',['a','b','c','d','e']);tasks=[]
    def handler(request):
        _,packet=input_packet(request);tasks.append(packet['task'])
        assert packet['task']=='T0_discovery'
        return response(two_groups(packet))
    with MemorySource([t],{'public.objects':[('A','Alpha','C','Charlie','active')]}) as source:
        result=discover(source,model=client(monkeypatch,handler))
    assert [rule.identity.key_columns for rule in result.rules]==[['a']]
    assert tasks==['T0_discovery']
    assert result.stats['unresolved_proposals']==1
    assert result.stats.get('deferred_proposals',0)==0
    assert any(issue['code']=='unregistered_attribute_semantics' and issue['status']=='unresolved'
        for issue in result.issues)


def test_semantic_refutation_offers_one_alternative_without_repeating_review(monkeypatch):
    t=table('objects',['a','b','c','d']);tasks=[]
    def handler(request):
        _,p=input_packet(request);tasks.append(p['task'])
        if p['task']=='semantic_review':
            return response({'proposition':p['semantic_proposition'],'decision':'refuted','basis':'data_inference',
                'evidence':[{'reference':p['metadata_digest']['columns'][0]['id'],'stance':'refutes'}]})
        assert p['task'] in ('T0_discovery','T5_revision')
        batch=group(p,'a','b')
        ids={c['name']:c['id'] for c in p['metadata_digest']['columns']}
        batch['proposals'].append({'kind':'wide_hierarchy','claim':{'group_id':'tree','levels':[
            {'position':1,'key':ids['c']},{'position':2,'key':ids['d']}]},'evidence_cited':[ids['c']]})
        return response(batch)
    with MemorySource([t],{'public.objects':[('A','Alpha','C','D')]}) as source:
        result=discover(source,model=client(monkeypatch,handler))
    assert [rule.identity.key_columns for rule in result.rules]==[['a']]
    assert tasks==['T0_discovery','semantic_review','T5_revision']
    assert len(result.pending_candidates)==1
    assert any(i['code']=='semantic_claim_refuted' and i['status']=='unresolved' for i in result.issues)


def test_same_batch_fact_value_cannot_become_business_attribute(monkeypatch):
    t=table('objects',['a','b','c','d','e','amount'])
    def handler(request):
        _,packet=input_packet(request)
        assert packet['task']=='T0_discovery'
        batch=two_groups(packet);batch['semantic_schema']={'properties':[custom_property()]}
        amount=next(c['id'] for c in packet['metadata_digest']['columns'] if c['name']=='amount')
        batch['proposals'][1]['claim']['members'].extend([
            {'column_id':amount,'role':'fact_value','exclude_from_projection':True},
            {'column_id':amount,'role':'semantic_attribute','property_name':'description'}])
        return response(batch)
    with MemorySource([t],{'public.objects':[('A','Alpha','C','Charlie','active',9999)]}) as source:
        result=discover(source,model=client(monkeypatch,handler))
    assert len(result.rules)==2
    for rule in result.rules:
        assert 'amount' not in rule.attributes.single_valued.values()
        assert 'amount' not in rule.attributes.multi_valued.values()
        assert 'amount' not in rule.identity.key_columns
        assert 'amount' in {item.column for item in rule.attributes.excluded}


@pytest.mark.parametrize('affects',[['identity'],['projection']])
def test_same_batch_exclusion_blocks_affected_sibling(monkeypatch,affects):
    t=table('objects',['a','b','c','d','e'])
    def handler(request):
        _,packet=input_packet(request)
        assert packet['task']=='T0_discovery'
        batch=two_groups(packet);batch['semantic_schema']={'properties':[custom_property()]}
        key=next(c['id'] for c in packet['metadata_digest']['columns'] if c['name']=='a')
        batch['proposals'].append({'kind':'exclusion','claim':{'exclusions':[{
            'column_id':key,'reason':'out_of_goal','affects':affects}]},'evidence_cited':[key]})
        return response(batch)
    with MemorySource([t],{'public.objects':[('A','Alpha','C','Charlie','active')]}) as source:
        result=discover(source,model=client(monkeypatch,handler))
    assert [rule.identity.key_columns for rule in result.rules]==[['c']]
    assert any(i['code']=='excluded_column_in_identity' for i in result.issues)


def test_real_fd_revision_cannot_hide_counterexample_in_expanded_identity(monkeypatch):
    t=table('objects',['code','label','custom'])
    prior=RuleSpec(rule_id='original',kind='column_group_entity',source={'source_id':'s','table':'objects'},
        identity={'key_columns':['code'],'entity_kind':'ReportItem'},attributes={'single_valued':{'label':'label'}})
    tasks=[]
    def handler(request):
        _,packet=input_packet(request);tasks.append(packet['task'])
        assert packet['task']=='T5_revision' and 'fd_violation' in packet['allowed_revision_codes']
        proposal=group(packet,'code','label',extra=[('custom',{'role':'semantic_attribute','property_name':'status'})])
        label=next(c['id'] for c in packet['metadata_digest']['columns'] if c['name']=='label')
        proposal['proposals'][0]['claim']['members'].append({'column_id':label,'role':'identity_code','in_identity':True})
        proposal['semantic_schema']={'properties':[custom_property()]}
        return response(proposal)
    with MemorySource([t],{'public.objects':[('K','First','x'),('K','Second','x')]}) as source:
        result=discover(source,model=client(monkeypatch,handler),revision_rules=[prior],
                        iteration_context={'codes':['fd_violation']})
    assert tasks==['T5_revision'] and not result.rules
    assert any(i['code']=='revision_masks_counterexample' for i in result.issues)


def test_same_batch_conflicting_interpretations_do_not_choose_first(monkeypatch):
    t=table('objects',['a','b','c','d','e']);tasks=[]
    def handler(request):
        _,packet=input_packet(request);tasks.append(packet['task'])
        if packet['task']=='T5_revision':
            assert packet['iteration_context']['rule_conflict_requests']
            return response({'task':packet['task'],'abstain':True})
        assert packet['task']=='T0_discovery'
        batch=two_groups(packet);batch['proposals'].extend(group(packet,'a','d')['proposals'])
        return response(batch)
    with MemorySource([t],{'public.objects':[('A','Alpha','C','Charlie','active')]}) as source:
        result=discover(source,model=client(monkeypatch,handler))
    assert not result.rules
    assert any(i['code']=='conflicting_rule_proposals' for i in result.issues)
    assert tasks==['T0_discovery','T5_revision']
    assert len(result.pending_candidates)==1 and len(result.pending_candidates[0].mappings)==2


@pytest.mark.parametrize('initial_error',['reference_hierarchy_level_required','relation_endpoint_kind_mismatch'])
def test_invalid_hierarchy_endpoint_closes_or_retains_a_gap_without_extra_model_call(monkeypatch,initial_error):
    t=table('objects',['parent','parent_name','child','child_name','center','center_name'])
    hierarchy=RuleSpec(rule_id='tree',kind='wide_hierarchy',source={'source_id':'s','table':'objects'},
        identity={'key_columns':['parent'],'entity_kind':'ReportItem','level_policy':'level_qualified'},
        hierarchy={'levels':[{'level':1,'code':'parent','label':'parent_name'},
                             {'level':2,'code':'child','label':'child_name'}],
                   'order_evidence':['user:hierarchy']},status='validated',validation_ref='v_tree')
    center=RuleSpec(rule_id='centers',kind='column_group_entity',source={'source_id':'s','table':'objects'},
        identity={'key_columns':['center'],'entity_kind':'Dimension'},status='validated',validation_ref='v_center')
    tasks=[]
    def handler(request):
        _,packet=input_packet(request);tasks.append(packet['task'])
        assert packet['task']=='T2_cross_table'
        ids={c['name']:c['id'] for c in packet['metadata_digest']['columns']}
        claim={'source_entity':'tree','target_entity':'centers','source_columns':[ids['child']],
               'target_columns':[ids['center']],'relation_kind':'assigned_to'}
        if initial_error=='relation_endpoint_kind_mismatch':claim.update(source_level=2,relation_kind='verified_reference')
        return response({'task':packet['task'],'semantic_schema':{'relations':[{'name':'assigned_to',
            'description':'The responsibility center assigned to this object.',
            'source_kinds':['ReportItem'],'target_kinds':['Dimension']}]},
            'proposals':[{'kind':'reference','claim':claim,'evidence_cited':[ids['child']]}]})
    with MemorySource([t],{'public.objects':[('A','Parent','B','Child','Z','Center')]}) as source:
        result=discover_references(source,[hierarchy,center],model=client(monkeypatch,handler))
    assert not result.rules and tasks==['T2_cross_table']
    status='rejected' if initial_error=='relation_endpoint_kind_mismatch' else 'unresolved'
    assert any(issue['code']==initial_error and issue['status']==status for issue in result.issues)
    assert result.relation_table_outcomes['public.objects']==('resolved' if status=='rejected' else 'unresolved')
