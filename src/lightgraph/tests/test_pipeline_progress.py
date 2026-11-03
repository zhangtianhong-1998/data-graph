"""Real pipeline and offline model boundaries publish counts, never observations."""
import json
from types import SimpleNamespace

import httpx
import pytest

from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.discovery.budget import BudgetAllocator, Cost
from lightgraph.discovery.models import ProposalBatch
from lightgraph.discovery.workflow import discover_with_agent
from lightgraph.errors import LightGraphError
from lightgraph.pipeline import run_pipeline
from lightgraph.rules import RuleEngine
from lightgraph.runtime.config import RunConfig
from lightgraph.runtime.progress import RunProgress, bind_progress, emit_progress
from lightgraph.sample import build_sample, sample_rules
from lightgraph.sources import MemorySource
from lightgraph.tests.test_model_streaming import model


CANARY='SOURCE_VALUE_MUST_NOT_ENTER_PROGRESS_09231'






@pytest.mark.parametrize('failure',[False,True])
def test_model_events_bracket_transport_and_keep_parent_phase_without_payloads(tmp_path,failure):
    path=tmp_path/'run_000000000000000000000002.progress.json'

    def handler(request):
        snapshot=json.loads(path.read_text(encoding='utf-8'))
        assert snapshot['stage']=='relations'
        assert snapshot['events'][-1]['code']=='model_call_started'
        assert snapshot['counters']['model_calls']==1
        assert snapshot['completed']==0 and snapshot['total']==2
        if failure:
            raise httpx.ReadError(CANARY,request=request)
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"ok":true}'},
            'finish_reason':'stop'}],'usage':{'prompt_tokens':12,'completion_tokens':3}})

    client=model(handler,stream=False,response_format="json_object")
    with bind_progress(RunProgress(path)):
        emit_progress('relations','table_started',completed=0,total=2,unit='tables')
        if failure:
            with pytest.raises(LightGraphError):
                client._request([{'role':'user','content':CANARY}],lambda message:json.loads(message['content']))
        else:
            assert client._request([{'role':'user','content':CANARY}],lambda message:json.loads(message['content']))=={'ok':True}
    text=path.read_text(encoding='utf-8')
    assert CANARY not in text and 'synthetic-secret' not in text
    state=json.loads(text)
    assert state['stage']=='relations'
    assert state['events'][-1]['code']==('model_call_failed' if failure else 'model_call_completed')
    assert state['counters']['model_calls']==1
    assert state['counters']['model_failures']==int(failure)
    if not failure:
        assert state['counters']['input_tokens']==12 and state['counters']['output_tokens']==3


def test_discovery_progress_counts_only_selected_tables_and_keeps_rejection_visible():
    tables=[TableSpec(source_id='s',name=name,columns=[ColumnSpec(name='code'),ColumnSpec(name='name')])
            for name in ('a','b','unselected')]
    events=[]

    class AbstainingModel:
        usage={'calls':0,'input_tokens':0,'output_tokens':0,'failures':0}
        allow_source_samples=False
        config=SimpleNamespace(allow_source_samples=False,max_prompt_bytes=1<<20)
        budget=BudgetAllocator(Cost(seconds=60,db_cu=100,llm_calls=10,llm_tokens=None,memory_bytes=1<<20))
        def explore(self,packet,tools,**kwargs):
            assert events[-1]['code']=='table_started'
            return ProposalBatch(task=packet['task'],abstain=True)

    with MemorySource(tables,{table.qualified_name:[] for table in tables}) as source, bind_progress(events.append):
        result=discover_with_agent(source,model=AbstainingModel(),table_refs={'public.a','public.b'})
    finished=[e for e in events if e['code']=='table_completed']
    assert [(e['table'],e['completed'],e['total']) for e in finished]==[('s.public.a',1,2),('s.public.b',2,2)]
    assert all(e['level']=='warning' for e in finished)
    assert not result.rules
