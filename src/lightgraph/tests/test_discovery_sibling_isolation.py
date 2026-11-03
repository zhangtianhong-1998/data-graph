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
    assert tasks.count('T5_revision')==(1 if decision=='supported' else 2)
    assert model.budget.snapshot()['reserved']=={}
