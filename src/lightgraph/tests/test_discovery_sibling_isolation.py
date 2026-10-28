"""Invalid candidates and data revisions preserve independently validated rules."""
import pytest

from lightgraph.discovery import discover
from lightgraph.pipeline import run_pipeline
from lightgraph.rules import RuleEngine
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import client, group, input_packet, response, table
from lightgraph.tests.test_compiled_proposal_contract import hierarchy


def review(packet, decision='supported'):
    return response({'proposition_ref':packet['semantic_proposition_ref'],
        'decision':decision,'basis':'data_inference','evidence':[{
            'reference':packet['metadata_digest']['columns'][0]['id'],
            'stance':'refutes' if decision=='refuted' else 'supports'}]})


def invalid_repair(packet):
    return response({'task':packet['task'],'proposals':[{'kind':'column_group','claim':{}}]})


@pytest.mark.parametrize('reverse',[False,True])
@pytest.mark.parametrize('decision',['supported','refuted'])
def test_fd_repair_failure_preserves_only_independently_reviewed_hierarchy(monkeypatch,reverse,decision):
    source_table=table('objects',['a','b','c','d']);tasks=[]
    def handler(request):
        _,packet=input_packet(request);tasks.append(packet['task'])
        if packet['task']=='semantic_review':return review(packet,decision)
        if packet['task']=='T0_discovery':
            batch=group(packet,'a','b','Metric')
            batch['proposals'].append(hierarchy(packet,{}))
            if reverse:batch['proposals'].reverse()
            return response(batch)
        return invalid_repair(packet)
    model=client(monkeypatch,handler);model.config.retries=0
    with MemorySource([source_table],{'public.objects':[
            ('A','First','P','C'),('A','Conflicting','C','G')]}) as source,GraphStore() as store:
        result=discover(source,model=model)
        assert [rule.kind for rule in result.rules]==(['wide_hierarchy'] if decision=='supported' else [])
        for rule in result.rules:
            assert RuleEngine(store).execute(source,rule).published
        graph=store.view({'public'})
        assert len(graph['nodes'])==(3 if decision=='supported' else 0)
        assert len(graph['edges'])==(2 if decision=='supported' else 0)
        assert all(node['kind']!='Metric' for node in graph['nodes'])
    assert any(issue['code']=='fd_violation' for issue in result.issues)
    assert any(issue['code']=='llm_schema_invalid' for issue in result.issues)
    assert tasks.count('T5_revision')==1
    assert model.budget.snapshot()['reserved']=={}


@pytest.mark.parametrize('guard',['none','refuted','excluded','cycle'])
def test_deferred_coverage_executes_valid_sibling_but_keeps_pending_work(monkeypatch,tmp_path,guard):
    source_table=table('objects',['a','b','c','d','e','f','g']);tasks=[]
    def handler(request):
        _,packet=input_packet(request);tasks.append(packet['task'])
        if packet['task']=='T0_discovery':return response(group(packet,'a','b','Dimension'))
        if packet['task']=='semantic_review':return review(packet,'refuted' if guard=='refuted' else 'supported')
        if packet['task']=='T1_table_discovery':
            batch=group(packet,'e','f','Metric',extra=[
                ('g',{'role':'semantic_attribute','property_name':'unknown_status'})])
            batch['proposals'].insert(0,hierarchy(packet,{}))
            if guard=='excluded':
                column=next(c['id'] for c in packet['metadata_digest']['columns'] if c['name']=='c')
                batch['proposals'].append({'kind':'exclusion','claim':{'exclusions':[
                    {'column_id':column,'reason':'out_of_goal','affects':['identity','projection']} ]},
                    'evidence_cited':[column]})
            return response(batch)
        if packet['task']=='T5_revision':return invalid_repair(packet)
        return response({'task':packet['task'],'abstain':True})
    model=client(monkeypatch,handler);model.config.retries=0
    rows=[('A','Alpha','P','C','E','Name','value'),
          ('A','Alpha','C','P' if guard=='cycle' else 'G','E','Name','value')]
    with MemorySource([source_table],{'public.objects':rows}) as source:
        report=run_pipeline(RunConfig(sources=[],output_dir=str(tmp_path),max_identity_reviews=0),
            [source],model=model)
    assert report.counts['business_nodes']==(4 if guard=='none' else 1),report.model_dump()
    assert report.counts['business_edges']==(2 if guard=='none' else 0)
    assert report.status!='COMPLETED'
    assert tasks.count('T1_table_discovery')==1
    assert any(issue['code']=='unregistered_attribute_semantics' and issue['status']=='deferred'
               for issue in report.issues)
    if guard=='cycle':
        # Only the actual data cycle can schedule a revision Agent.
        assert tasks.count('T5_revision')==1
        assert any(issue['code']=='llm_schema_invalid' for issue in report.issues)
    else:
        assert 'T5_revision' not in tasks
        assert not any(issue['code']=='llm_schema_invalid' for issue in report.issues)
    assert any(issue['code']=='unexplained_columns' and issue['status']=='deferred'
               and issue['count']==(3 if guard=='none' else 5) for issue in report.issues)
    with GraphStore(tmp_path/'graph.sqlite') as store:
        states=[row['state'] for row in store.connection.execute(
            "SELECT state FROM iteration_work WHERE task='coverage_discovery'")]
        assert states and set(states)=={'deferred'}
        graph=store.view({'public'})
        assert all(node['kind']!='Metric' for node in graph['nodes'])
        if guard=='none':
            assert len([edge for edge in graph['edges'] if edge['relation']=='parent_of'])==2
    assert model.budget.snapshot()['reserved']=={}
