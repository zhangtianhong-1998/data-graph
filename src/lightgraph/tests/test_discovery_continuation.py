"""Offline regressions for open discovery work and committed graph export."""
import json
import pytest

from lightgraph.errors import LightGraphError
from lightgraph.pipeline import run_pipeline
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import client,group,input_packet,response,table


def test_relation_abstention_retries_t2_without_repeating_entity_discovery(monkeypatch,tmp_path):
    tables=[table('a',['key','label']),table('b',['key','label'])]
    rows={'public.a':[('A','Alpha')],'public.b':[('B','Beta')]}
    tasks=[]
    def handler(request):
        _,p=input_packet(request);tasks.append(p['task'])
        if p['task']=='T0_discovery':return response(group(p,'key','label'))
        return response({'task':p['task'],'abstain':True})
    config=RunConfig(sources=[],output_dir=str(tmp_path),max_identity_reviews=0)
    for index in range(3):
        tasks.clear()
        with MemorySource(tables,rows) as source:
            report=run_pipeline(config,[source],model=client(monkeypatch,handler))
        assert report.counts['business_nodes']==2
        assert report.counts['business_edges']==0
        assert tasks.count('T2_cross_table')==2
        assert tasks.count('T0_discovery')==(2 if index==0 else 0)
        assert report.counts['work_unresolved']==2
        assert any(i['code']=='relation_discovery_pending' for i in report.issues)


def test_abstention_does_not_cache_empty_table_as_complete(monkeypatch,tmp_path):
    t=table('t',['key','label']);phase={'empty':True};tasks=[]
    def handler(request):
        _,p=input_packet(request);tasks.append(p['task'])
        return response({'task':p['task'],'abstain':True} if phase['empty'] else group(p,'key','label'))
    config=RunConfig(sources=[],output_dir=str(tmp_path),max_identity_reviews=0)
    with MemorySource([t],{'public.t':[]}) as source:
        first=run_pipeline(config,[source],model=client(monkeypatch,handler))
    assert first.counts['business_nodes']==0
    phase['empty']=False;tasks.clear()
    with MemorySource([t],{'public.t':[('K','Name')]}) as source:
        second=run_pipeline(config,[source],model=client(monkeypatch,handler))
    assert tasks==['T0_discovery']
    assert second.counts['business_nodes']==1
    tasks.clear()
    with MemorySource([t],{'public.t':[('K','Name')]}) as source:
        third=run_pipeline(config,[source],model=client(monkeypatch,handler))
    assert not tasks and third.counts['business_nodes']==1


def test_entity_repair_enables_t2_before_publication(monkeypatch,tmp_path):
    t=table('multi',['x','x_name','realm','y','y_name'])
    config=RunConfig(sources=[],output_dir=str(tmp_path),scopes=['public','A','B'],max_identity_reviews=0)
    phase={'run':1};tasks=[]
    def handler(request):
        _,p=input_packet(request);tasks.append(p['task'])
        if p['task']=='T0_discovery':
            batch=group(p,'x','x_name','Metric')
            batch['proposals']+=group(p,'y','y_name','Measure',namespace='realm')['proposals']
            return response(batch)
        if p['task']=='T5_revision':return response(group(p,'x','x_name','Metric',namespace='realm'))
        if p['task']=='semantic_review':
            return response({'proposition_ref':p['semantic_proposition_ref'],'decision':'supported',
                'basis':'data_inference','evidence':[{'reference':p['metadata_digest']['columns'][0]['id'],'stance':'supports'}]})
        if p['task']=='T2_cross_table' and phase['run']>1:
            bindings={b['entity_kind']:b for b in p['entity_bindings']}
            ids={c['name']:c['id'] for c in p['metadata_digest']['columns']}
            return response({'task':p['task'],'proposals':[{'kind':'reference','claim':{
                'source_entity':bindings['Metric']['rule_id'],'target_entity':bindings['Measure']['rule_id'],
                'source_columns':[ids[c] for c in bindings['Metric']['key_columns']],
                'target_columns':[ids[c] for c in bindings['Measure']['key_columns']],
                'relation_kind':'uses_measure'},'evidence_cited':[ids['x']]}]})
        return response({'task':p['task'],'abstain':True})
    rows=[('X','old','A','Y1','first-y')]
    with MemorySource([t],{'public.multi':rows}) as source:
        run_pipeline(config,[source],model=client(monkeypatch,handler))
    rows+=[('X','changed','B','Y2','new-y')];phase['run']=2;tasks.clear()
    with MemorySource([t],{'public.multi':rows}) as source:
        repaired=run_pipeline(config,[source],model=client(monkeypatch,handler))
    assert tasks.index('T5_revision')<tasks.index('T2_cross_table')
    assert repaired.counts['business_nodes']==4 and repaired.counts['business_edges']==2
    tasks.clear()
    with MemorySource([t],{'public.multi':rows}) as source:
        repeated=run_pipeline(config,[source],model=client(monkeypatch,handler))
    assert not tasks,repeated.model_dump()


def test_unexplained_columns_trigger_bounded_followup_preserving_first_group(monkeypatch,tmp_path):
    t=table('wide',['code','name','center','caption']);tasks=[]
    def handler(request):
        _,p=input_packet(request);tasks.append(p['task'])
        if p['task']=='T0_discovery':return response(group(p,'code','name','Metric'))
        if p['task']=='T1_table_discovery':
            assert p['iteration_context']['coverage_columns']==['center','caption']
            assert p['iteration_context']['known_rules']
            return response(group(p,'center','caption','Dimension'))
        return response({'task':p['task'],'abstain':True})
    config=RunConfig(sources=[],output_dir=str(tmp_path),max_identity_reviews=0)
    with MemorySource([t],{'public.wide':[('K','Metric','C','Center')]}) as source:
        report=run_pipeline(config,[source],model=client(monkeypatch,handler))
    assert report.counts['business_nodes']==2,report.model_dump()
    assert tasks.count('T0_discovery')==1 and tasks.count('T1_table_discovery')==1
    assert report.counts['coverage_discovery_tasks']==1
    with GraphStore(tmp_path/'graph.sqlite') as store:
        assert len([r for r in store.get_rules() if r.kind=='column_group_entity'])==2


def test_post_publication_failure_still_exports_committed_graph(monkeypatch,tmp_path):
    import lightgraph.pipeline as pipeline
    t=table('one',['key','label'])
    def fail(*args,**kwargs):raise LightGraphError('wall_time_budget_exhausted')
    monkeypatch.setattr(pipeline,'publish_documents',fail)
    config=RunConfig(sources=[],output_dir=str(tmp_path),max_identity_reviews=0)
    with MemorySource([t],{'public.one':[('K','Name')]}) as source:
        report=run_pipeline(config,[source],model=client(monkeypatch,lambda r:response(group(input_packet(r)[1],'key','label'))))
    assert report.status=='PARTIAL'
    assert report.counts['business_nodes']==1
    assert report.validation['graph_export']=='written'
    assert (tmp_path/'viewer.html').is_file()
    graph=json.loads((tmp_path/'graph.json').read_text())
    assert any(n['label']=='Name' and n['layer']=='business' for n in graph['nodes'])


def test_new_role_occurrence_reaches_revision_without_persisting_source_keys(monkeypatch,tmp_path):
    t=table('people',['person','person_name','other','other_name'])
    marker='PRIVATE_PERSON_KEY_7239';packets=[]
    def handler(request):
        _,p=input_packet(request);packets.append(p)
        if p['task'] in ('T0_discovery','T5_revision'):
            b=group(p,'person','person_name')
            b['proposals']+=group(p,'other','other_name')['proposals']
            return response(b)
        return response({'task':p['task'],'abstain':True})
    config=RunConfig(sources=[],output_dir=str(tmp_path),max_identity_reviews=0)
    with MemorySource([t],{'public.people':[(marker,'Person',marker,'Person')]}) as source:
        report=run_pipeline(config,[source],model=client(monkeypatch,handler))
    revisions=[p for p in packets if p['task']=='T5_revision']
    assert len(revisions)==1,report.model_dump()
    work=revisions[0]['iteration_context']['work_items'][0]
    assert work['pattern_codes']==['unexplained_role_occurrence']
    assert work['column_refs']==['s.public.people.other','s.public.people.person']
    assert marker not in json.dumps(work)
    assert any(i['code']=='unexplained_role_occurrence' for i in report.issues)
    with GraphStore(tmp_path/'graph.sqlite') as store:
        saved=[dict(r) for r in store.connection.execute('SELECT * FROM iteration_work')]
        assert marker not in json.dumps(saved)
        assert any(r['task']=='review_pattern' and r['state']=='unresolved' for r in saved)


def test_rule_identity_includes_kind_and_namespace_without_attribute_id_churn():
    from lightgraph.discovery.engine import _logical_entity_rule_id
    t=table('objects',['tenant','code','name'])
    first=_logical_entity_rule_id(t,'column_group_entity','Metric',['tenant','code'],['tenant'])
    assert first!=_logical_entity_rule_id(t,'column_group_entity','Measure',['tenant','code'],['tenant'])
    assert first!=_logical_entity_rule_id(t,'column_group_entity','Metric',['tenant','code'],[])
    assert first==_logical_entity_rule_id(t,'column_group_entity','Metric',['tenant','code'],['tenant'])


def test_superseded_relation_task_reactivates_when_endpoint_set_returns():
    from lightgraph.contracts import ExtractionGoal,SourceRef
    from lightgraph.runtime.iteration import IterationLedger
    with GraphStore() as store:
        ledger=IterationLedger(store,ExtractionGoal(),['public'])
        source=SourceRef(source_id='s',table='t')
        a=ledger.enqueue(source,task='relation_discovery',context_version='endpoints_A')
        ledger.transition(a,'running');ledger.transition(a,'resolved')
        b=ledger.enqueue(source,task='relation_discovery',context_version='endpoints_B')
        ledger.supersede_previous(b);ledger.transition(b,'running');ledger.transition(b,'resolved')
        returned=ledger.enqueue(source,task='relation_discovery',context_version='endpoints_A')
        assert returned.state=='pending'
        ledger.supersede_previous(returned);ledger.transition(returned,'running')
        assert returned.attempts==2


def test_role_event_can_discover_relation_without_changing_entity_rules(monkeypatch,tmp_path):
    t=table('roles',['person','name','related','related_name']);calls=[]
    def handler(request):
        _,p=input_packet(request);calls.append(p)
        if p['task'] in ('T0_discovery','T5_revision'):
            batch=group(p,'person','name')
            batch['proposals']+=group(p,'related','related_name')['proposals']
            return response(batch)
        if p['task']=='T2_cross_table':
            assert p['iteration_context']['role_occurrences']
            ids={c['name']:c['id'] for c in p['metadata_digest']['columns']}
            bindings={b['key_columns'][0]:b['rule_id'] for b in p['entity_bindings']}
            return response({'task':p['task'],'proposals':[{'kind':'reference','claim':{
                'source_entity':bindings['person'],'target_entity':bindings['related'],
                'source_columns':[ids['person']],'target_columns':[ids['related']],
                'relation_kind':'parent_of'},'evidence_cited':[ids['person']]}]})
        if p['task']=='semantic_review':
            return response({'proposition_ref':p['semantic_proposition_ref'],'decision':'supported',
                'basis':'data_inference','evidence':[{'reference':p['metadata_digest']['columns'][0]['id'],'stance':'supports'}]})
        return response({'task':p['task'],'abstain':True})
    config=RunConfig(sources=[],output_dir=str(tmp_path),max_identity_reviews=0)
    with MemorySource([t],{'public.roles':[('P1','One','P2','Two'),('P2','Two','P3','Three')]}) as source:
        report=run_pipeline(config,[source],model=client(monkeypatch,handler))
    assert report.counts['business_edges']==2,report.model_dump()
    assert sum(p['task']=='T2_cross_table' for p in calls)==1
    assert sum(p['task']=='T5_revision' for p in calls)==1
    with GraphStore(tmp_path/'graph.sqlite') as store:
        assert all(r.revision==1 for r in store.get_rules() if r.kind=='column_group_entity')


def test_failed_followup_rule_cannot_complete_column_coverage(monkeypatch,tmp_path):
    t=table('wide',['code','name','extra','caption'])
    def handler(request):
        _,p=input_packet(request)
        if p['task']=='T0_discovery':return response(group(p,'code','name'))
        if p['task']=='T1_table_discovery':return response(group(p,'extra','caption','Dimension'))
        return response({'task':p['task'],'abstain':True})
    config=RunConfig(sources=[],output_dir=str(tmp_path),max_identity_reviews=0)
    with MemorySource([t],{'public.wide':[('K','Name',None,'Invalid object')]}) as source:
        report=run_pipeline(config,[source],model=client(monkeypatch,handler))
    assert report.counts['business_nodes']==1,report.model_dump()
    assert any(i['code']=='unexplained_columns' and i['count']==2 for i in report.issues)
    with GraphStore(tmp_path/'graph.sqlite') as store:
        states=[r['state'] for r in store.connection.execute("SELECT state FROM iteration_work WHERE task='coverage_discovery'")]
        assert states and 'resolved' not in states


@pytest.mark.parametrize('code', [
    'model_structured_output_rejected', 'model_output_schema_missing',
    'model_response_format_invalid',
])
def test_structured_output_failure_remains_pending_for_later_run(monkeypatch, tmp_path, code):
    t = table('format_retry', ['key', 'label'])
    tasks = []
    def failed(request):
        _, packet = input_packet(request)
        tasks.append(packet['task'])
        raise LightGraphError(code)
    config = RunConfig(sources=[], output_dir=str(tmp_path), max_identity_reviews=0)
    rows = {'public.format_retry': [('K', 'Name')]}
    with MemorySource([t], rows) as source:
        first = run_pipeline(config, [source], model=client(monkeypatch, failed))
    assert first.status == 'PARTIAL'
    assert first.counts['business_nodes'] == 0
    assert first.counts['work_deferred'] == 1
    assert any(issue['code'] == code for issue in first.issues)
    tasks.clear()
    def recovered(request):
        _, packet = input_packet(request)
        tasks.append(packet['task'])
        return response(group(packet, 'key', 'label'))
    with MemorySource([t], rows) as source:
        second = run_pipeline(config, [source], model=client(monkeypatch, recovered))
    assert tasks == ['T0_discovery']
    assert second.counts['business_nodes'] == 1
