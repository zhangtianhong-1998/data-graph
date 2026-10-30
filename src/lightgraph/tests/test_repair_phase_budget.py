"""Exploration failure must leave complete core results publishable."""
from copy import deepcopy
from lightgraph.discovery.coverage import attribute_enrichment
from lightgraph.pipeline import run_pipeline
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.tests.test_agent_pipeline import client, group, input_packet, response, table
from lightgraph.tests.test_rules_engine import wide_rule


def test_exploration_deadline_does_not_abort_validated_core(monkeypatch,tmp_path):
    import lightgraph.sources.budget as source_budget
    import lightgraph.discovery.budget as model_budget
    import lightgraph.pipeline as pipeline
    import time
    from types import SimpleNamespace
    clock=[0.0]
    timer=SimpleNamespace(monotonic=lambda:time.monotonic()+clock[0])
    for module in (source_budget,model_budget,pipeline):
        monkeypatch.setattr(module,'time',timer)
    t=table('t',['x','x_name','y','y_name'])
    stages=[]
    def handler(request):
        _,packet=input_packet(request);stages.append(packet['task'])
        if packet['task']=='T0_discovery':
            batch=group(packet,'x','x_name','Metric')
            batch['proposals']+=group(packet,'y','y_name','Measure')['proposals']
            return response(batch)
        # Expire exploration, but leave the reserved execution/publication time.
        clock[0]=80.0
        return response({'task':packet['task'],'abstain':True})
    config=RunConfig(sources=[],output_dir=str(tmp_path),max_run_seconds=100,max_identity_reviews=0)
    with MemorySource([t],{'public.t':[('x','指标','y','度量')]}) as source:
        report=run_pipeline(config,[source],model=client(monkeypatch,handler))
    assert 'T2_cross_table' in stages
    assert report.counts['business_nodes']==2,report.model_dump()
    assert len(report.rules_published)==2
    assert report.validation['graph_export']=='written'


def test_coverage_can_add_english_alias_without_new_identity(monkeypatch,tmp_path):
    t=table('t',['code','name','english']);tasks=[]
    def handler(request):
        _,packet=input_packet(request);tasks.append(packet['task'])
        extra=[('english',{'role':'alias','property_name':'english_name'})] if packet['task']=='T1_table_discovery' else []
        return response(group(packet,'code','name','Metric',extra=extra))
    with MemorySource([t],{'public.t':[('1','应收账款占用','Receivables') ]}) as source:
        report=run_pipeline(RunConfig(sources=[],output_dir=str(tmp_path),max_identity_reviews=0),
                            [source],model=client(monkeypatch,handler))
    assert tasks==['T0_discovery','T1_table_discovery']
    assert report.counts['business_nodes']==1,report.model_dump()
    assert report.counts['coverage_attribute_revisions']==1
    from lightgraph.store import GraphStore
    with GraphStore(tmp_path/'graph.sqlite') as store:
        node=next(n for n in store.view({'public'})['nodes'] if n['layer']=='business')
        assert node['properties']['english_name']=='Receivables'


def test_enrichment_keeps_prior_aliases_and_rejects_identity_or_topology_change():
    rule=wide_rule(); candidate=deepcopy(rule)
    candidate.hierarchy.levels[0].aliases=['english']
    enriched=attribute_enrichment(rule,candidate)
    assert enriched.hierarchy.levels[0].aliases==['english']
    assert enriched.revision==2
    assert not rule.hierarchy.levels[0].aliases
    candidate.identity.key_columns=['other']
    assert attribute_enrichment(rule,candidate) is None
    candidate=deepcopy(rule);candidate.hierarchy.allow_dag=True
    assert attribute_enrichment(rule,candidate) is None
