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


def test_discovery_feedback_survives_restart_without_source_values_or_task_overwrite(tmp_path):
    path=tmp_path/'feedback.sqlite'; source=SourceRef(source_id='s',table='people')
    canary='REJECTED_SOURCE_VALUE_NOT_RETAINED'
    with GraphStore(path) as store:
        ledger=IterationLedger(store,ExtractionGoal(),['public'])
        ledger.record_discovery_feedback([source],[
            {'code':'semantic_claim_refuted','table':'public.people','message':canary,'proposal':{'label':canary}},
            {'code':canary}, {'code':'semantic_claim_unresolved','table':'public.other'}],task='metadata_discovery')
        ledger.record_discovery_feedback([source],[],task='relation_discovery')
    with GraphStore(path) as store:
        ledger=IterationLedger(store,ExtractionGoal(),['public'])
        item=ledger.enqueue(source,task='metadata_discovery',context_version='schema1')
        feedback=ledger.context([item],[],iteration=0)['prior_discovery_feedback']
        assert feedback==[{'source':source.model_dump(mode='json',by_alias=True),
            'task':'metadata_discovery','codes':['semantic_claim_refuted']}]
        assert canary not in json.dumps([dict(row) for row in store.connection.execute('SELECT * FROM discovery_feedback')])
        ledger.record_discovery_feedback([source],[],task='metadata_discovery')
        assert ledger.context([item],[],iteration=1)['prior_discovery_feedback']==[]


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
