"""Cross-session repair, bounded progress and source-free task state."""
import json
import pytest

from lightgraph.contracts import ExtractionGoal, SourceRef
from lightgraph.errors import LightGraphError
from lightgraph.pipeline import run_pipeline
from lightgraph.runtime.config import RunConfig
from lightgraph.runtime.iteration import IterationLedger
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import client, group, input_packet, response, table


def test_ledger_freezes_goal_and_recovers_interrupted_work(tmp_path):
    path=tmp_path/'graph.sqlite'
    with GraphStore(path) as store:
        ledger=IterationLedger(store,ExtractionGoal(purpose='people'),['public'])
        item=ledger.enqueue(SourceRef(source_id='s',table='people'),codes=['fd_violation'],context_version='schema1')
        ledger.transition(item,'running')
        revision=ledger.goal_revision
        with pytest.raises(LightGraphError,match='unregistered_iteration_pattern'):
            ledger.enqueue(item.source,codes=['raw_source_value'],context_version='schema1')
    with GraphStore(path) as store:
        ledger=IterationLedger(store,ExtractionGoal(purpose='people'),['public'])
        assert ledger.goal_revision==revision
        assert ledger.items()[0].state=='deferred'
        again=ledger.enqueue(item.source,codes=['fd_violation'],context_version='schema1')
        assert again.work_id==item.work_id and again.state=='pending'
        ledger.transition(again,'running');ledger.transition(again,'unresolved')
        same=ledger.enqueue(item.source,codes=['fd_violation'],context_version='schema1')
        assert same.state=='unresolved'
        assert ledger.unresolved_tables('s',{'public.people':'schema1'})=={'public.people'}
        assert ledger.unresolved_tables('s',{'public.people':'schema2'})==set()
        updated=IterationLedger(store,ExtractionGoal(purpose='organizations'),['public'])
        assert updated.goal_revision==revision+1 and updated.items()==[]


def test_cached_data_counterexample_starts_new_agent_and_then_reuses(monkeypatch,tmp_path):
    t=table('opaque',['x','y','realm'])
    config=RunConfig(sources=[],output_dir=str(tmp_path/'out'),scopes=['public','A','B'],goal=ExtractionGoal(purpose='Find reusable objects'))
    packets=[]
    def handler(request):
        _,p=input_packet(request);packets.append(p)
        if p['task']=='T5_revision':return response(group(p,'x','y',namespace='realm'))
        if p['task']=='T0_discovery':return response(group(p,'x','y'))
        return response({'task':p['task'],'abstain':True})
    with MemorySource([t],{'public.opaque':[('K','First','A')]}) as source:
        first=run_pipeline(config,[source],model=client(monkeypatch,handler))
    assert first.status=='COMPLETED_WITH_GAPS'
    assert any(i['code']=='unexplained_columns' for i in first.issues)
    packets.clear()
    rows=[('K','First','A'),('K','Second','B')]
    with MemorySource([t],{'public.opaque':rows}) as source:
        second=run_pipeline(config,[source],model=client(monkeypatch,handler))
    assert second.rules_published,second.model_dump()
    assert any(p['task']=='T5_revision' for p in packets)
    assert all(p['goal']['purpose']==config.goal.purpose for p in packets)
    reviews=[p for p in packets if p.get('iteration_context',{}).get('iteration',0)>0]
    assert reviews and reviews[0]['iteration_context']['known_rules']
    assert second.counts['work_unresolved']==0
    packets.clear()
    with MemorySource([t],{'public.opaque':rows}) as source:
        third=run_pipeline(config,[source],model=client(monkeypatch,handler))
    assert not packets,third.model_dump()
    with GraphStore(tmp_path/'out/graph.sqlite') as store:
        labels={n['label'] for n in store.view({'public','A','B'})['nodes'] if n['layer']=='business'}
        assert labels=={'First','Second'}
        tasks=[dict(row) for row in store.connection.execute('SELECT * FROM iteration_work')]
        assert 'First' not in json.dumps(tasks) and 'Second' not in json.dumps(tasks)


def test_unresolved_counterexample_does_not_repeat_same_agent_task(monkeypatch,tmp_path):
    t=table('opaque',['x','y','realm'])
    config=RunConfig(sources=[],output_dir=str(tmp_path/'out'))
    packets=[]
    def handler(request):
        _,p=input_packet(request);packets.append(p)
        if p['task']=='T0_discovery':return response(group(p,'x','y'))
        return response({'task':p['task'],'abstain':True})
    with MemorySource([t],{'public.opaque':[('K','First','A')]}) as source:
        run_pipeline(config,[source],model=client(monkeypatch,handler))
    rows=[('K','First','A'),('K','Second','B')]
    with MemorySource([t],{'public.opaque':rows}) as source:
        failed=run_pipeline(config,[source],model=client(monkeypatch,handler))
    assert failed.counts['work_unresolved']==2,failed.model_dump()  # FD review and remaining realm column
    packets.clear()
    with MemorySource([t],{'public.opaque':rows}) as source:
        repeated=run_pipeline(config,[source],model=client(monkeypatch,handler))
    assert not packets,repeated.model_dump()  # failed entities need T5; unchanged unresolved work is not re-asked
    with GraphStore(tmp_path/'out/graph.sqlite') as store:
        assert not [n for n in store.view({'public'})['nodes'] if n['layer']=='business']
        assert any(not h['valid'] for h in store.heads() if not h['logical_rule_id'].startswith(('catalog:','discovery:')))
    # The old interpretation may become valid again without an Agent call.
    packets.clear()
    with MemorySource([t],{'public.opaque':[('K','First','A')]}) as source:
        recovered=run_pipeline(config,[source],model=client(monkeypatch,handler))
    assert [p['task'] for p in packets]==['T1_table_discovery']
    assert recovered.counts['work_unresolved']==1,recovered.model_dump()  # only coverage remains open


def test_excel_import_obeys_configured_working_memory(tmp_path):
    import openpyxl
    from lightgraph.runtime.config import SourceConfig, open_sources
    book=openpyxl.Workbook();sheet=book.active
    sheet.append(['code','name'])
    for i in range(100):sheet.append([str(i),'label'*20])
    path=tmp_path/'input.xlsx';book.save(path);book.close()
    config=RunConfig(sources=[SourceConfig(id='excel',kind='excel_workbook',path=str(path))],max_working_bytes=1024)
    with pytest.raises(LightGraphError) as raised:open_sources(config)
    assert raised.value.code in ('memory_budget_exhausted','excel_expansion_limit')


def test_repairing_one_column_group_keeps_sibling_new_entities(monkeypatch,tmp_path):
    """A whole-table retry must not discard a successful unrelated projection."""
    t=table('multi',['x','x_name','realm','y','y_name'])
    config=RunConfig(sources=[],output_dir=str(tmp_path/'out'),scopes=['public','A','B'])
    calls=[]
    def handler(request):
        _,p=input_packet(request);calls.append(p['task'])
        if p['task']=='T0_discovery':
            batch=group(p,'x','x_name')
            batch['proposals']+=group(p,'y','y_name')['proposals']
            return response(batch)
        if p['task']=='T5_revision':return response(group(p,'x','x_name',namespace='realm'))
        return response({'task':p['task'],'abstain':True})
    with MemorySource([t],{'public.multi':[('X','old','A','Y1','first-y')]}) as source:
        initial=run_pipeline(config,[source],model=client(monkeypatch,handler))
    assert initial.status=='COMPLETED_WITH_GAPS'
    assert any(i['code']=='relation_discovery_pending' for i in initial.issues)
    rows=[('X','old','A','Y1','first-y'),('X','changed','B','Y2','new-y')]
    calls.clear()
    with MemorySource([t],{'public.multi':rows}) as source:
        repaired=run_pipeline(config,[source],model=client(monkeypatch,handler))
    assert 'T5_revision' in calls,repaired.model_dump()
    assert len(repaired.rules_published)==2,repaired.model_dump()
    with GraphStore(tmp_path/'out/graph.sqlite') as store:
        labels={n['label'] for n in store.view({'public','A','B'})['nodes'] if n['layer']=='business'}
        assert labels=={'old','changed','first-y','new-y'}
    calls.clear()
    with MemorySource([t],{'public.multi':rows}) as source:
        repeated=run_pipeline(config,[source],model=client(monkeypatch,handler))
    assert calls==['T2_cross_table'],repeated.model_dump()  # entity rules are reused; abstained relations remain open
