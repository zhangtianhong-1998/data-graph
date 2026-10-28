"""Bad reference contracts are deferred without restarting the discovery Agent."""
from copy import deepcopy

import pytest

from lightgraph.discovery.workflow import discover_references_with_agent
from lightgraph.rules import RuleEngine
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import client, input_packet, response
from lightgraph.tests.test_reference_self_link_review import inputs


@pytest.mark.parametrize('bad_first',[False,True])
@pytest.mark.parametrize('error',[
    'unregistered_relation','relation_endpoint_kind_mismatch',
    'reference_arity_mismatch','reference_level_on_nonhierarchy','reference_missing'])
def test_one_t2_batch_records_invalid_reference_and_publishes_valid_sibling(monkeypatch,error,bad_first):
    table,base=inputs();calls=[]
    def handler(request):
        _,packet=input_packet(request);calls.append(packet['task'])
        if packet['task']=='semantic_review':
            assert packet['semantic_proposition']['claim']['relation_kind']=='assigned_to'
            return response({'proposition_ref':packet['semantic_proposition_ref'],
                'decision':'supported','basis':'data_inference','evidence':[
                    {'reference':'backend_endpoint_verification','stance':'supports'}]})
        assert packet['task']=='T2_cross_table' and calls.count('T2_cross_table')==1
        ids={item['name']:item['id'] for item in packet['metadata_digest']['columns']}
        valid={'kind':'reference','claim':{'source_entity':'objects','target_entity':'other',
            'source_columns':[ids['code']],'target_columns':[ids['other']],
            'relation_kind':'assigned_to'},'evidence_cited':[ids['code']]}
        bad=deepcopy(valid)
        if error=='unregistered_relation':bad['claim']['relation_kind']='unknown_relationship'
        elif error=='relation_endpoint_kind_mismatch':bad['claim']['relation_kind']='verified_reference'
        elif error=='reference_arity_mismatch':bad['claim']['target_columns']=[ids['other'],ids['target']]
        elif error=='reference_missing':bad['claim']['target_columns']=[ids['target']]
        else:bad['claim']['source_level']=1
        return response({'task':packet['task'],'semantic_schema':{'relations':[
            {'name':'assigned_to','description':'The business object is assigned to this dimension.',
             'source_kinds':['ReportItem'],'target_kinds':['Dimension']}]},
            'proposals':[bad,valid] if bad_first else [valid,bad]})
    with MemorySource([table],{'public.objects':[('A','Alpha','unused','D')]}) as source,GraphStore() as store:
        engine=RuleEngine(store)
        for rule in base:assert engine.execute(source,rule).published
        result=discover_references_with_agent(source,store.get_rules(),model=client(monkeypatch,handler))
        assert len(result.rules)==1
        status='unresolved' if error=='reference_missing' else 'deferred'
        assert result.relation_table_outcomes[table.qualified_name]==status
        assert any(item['code']==error and item['status']==status for item in result.issues)
        assert result.stats.get('deferred_proposals',0)==int(status=='deferred')
        assert result.stats.get('compile_repair_rounds',0)==0
        assert result.stats.get('recovered_proposals',0)==0
        assert engine.execute(source,result.rules[0]).published
        graph=store.view({'public'})
        assert len(graph['nodes'])==2 and len(graph['edges'])==1
        assert graph['edges'][0]['relation']=='assigned_to'
    assert calls==['T2_cross_table','semantic_review']


@pytest.mark.parametrize('decision,code',[
    ('refuted','semantic_claim_refuted'),('unresolved','semantic_claim_unresolved')])
def test_single_pass_does_not_retry_or_publish_negative_relation_meaning(monkeypatch,decision,code):
    from lightgraph.tests.test_reference_self_link_review import handler_factory
    table,base=inputs();packets=[]
    with MemorySource([table],{'public.objects':[('A','Alpha','A','D')]}) as source,GraphStore() as store:
        engine=RuleEngine(store)
        for rule in base:assert engine.execute(source,rule).published
        result=discover_references_with_agent(source,store.get_rules(),
            model=client(monkeypatch,handler_factory(True,decision,packets)))
        assert result.rules==[] and not store.view({'public'})['edges']
        assert any(issue['code']==code for issue in result.issues)
    assert [packet['task'] for packet in packets]==['T2_cross_table','semantic_review']
