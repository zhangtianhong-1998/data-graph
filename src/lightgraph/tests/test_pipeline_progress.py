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


def test_pipeline_progress_is_visible_before_rule_execution_and_tracks_publications(tmp_path, monkeypatch):
    progress=tmp_path/'run_000000000000000000000001.progress.json'
    tables,data=build_sample(20)
    rules=sample_rules()
    observed=[]
    original=RuleEngine.execute

    def during_execution(self, adapter, rule, **kwargs):
        state=json.loads(progress.read_text(encoding='utf-8'))
        event=state['events'][-1]
        assert event['code']=='rule_started' and event['rule_id']==rule.rule_id
        assert state['stage']=='extraction'
        assert state['completed']<state['total']
        observed.append(rule.rule_id)
        return original(self,adapter,rule,**kwargs)

    monkeypatch.setattr(RuleEngine,'execute',during_execution)
    config=RunConfig(sources=[],output_dir=str(tmp_path/'output'),scopes=['A','B','public'])
    with MemorySource(tables,data) as source, bind_progress(RunProgress(progress)):
        report=run_pipeline(config,[source],rules=rules)
    state=json.loads(progress.read_text(encoding='utf-8'))
    events=state['events']
    assert len(observed)==len(rules)
    assert len([e for e in events if e['code']=='rule_completed'])==len(rules)
    assert set(observed)==set(report.rules_published)
    assert state['counters']['published_rules']==len(report.rules_published)
    metadata=next(e for e in events if e['stage']=='metadata' and e['code']=='stage_completed')
    assert metadata['completed']==metadata['total']==len(tables)
    assert any(e['code']=='publication_started' for e in events)
    assert all('message' not in e for e in events)


def test_rule_rejection_still_has_a_terminal_event_without_claiming_publication(tmp_path):
    tables,data=build_sample(10)
    rule=sample_rules()[0]
    rule.source.source_id='absent_source'
    events=[]
    with MemorySource(tables,data) as source, bind_progress(events.append):
        report=run_pipeline(RunConfig(sources=[],output_dir=str(tmp_path/'output')),[source],rules=[rule])
    rejected=[e for e in events if e['code']=='rule_rejected']
    assert len(rejected)==1 and rejected[0]['rule_id']==rule.rule_id
    assert rejected[0]['level']=='warning'
    assert rule.rule_id in report.rules_skipped and not report.rules_published


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


def test_publication_failure_does_not_emit_publication_completed(tmp_path,monkeypatch):
    from lightgraph.store import GraphStore
    tables,data=build_sample(10)
    rules=sample_rules()
    rule_ids={rule.rule_id for rule in rules}
    original=GraphStore.publish_partitions
    attempted=[]

    def reject_business(self, completions, **kwargs):
        if any(self._publication(item.publication_id)['logical_rule_id'] in rule_ids for item in completions):
            attempted.append(True)
            raise LightGraphError('publication_failed')
        return original(self,completions,**kwargs)

    monkeypatch.setattr(GraphStore,'publish_partitions',reject_business)
    events=[]
    with MemorySource(tables,data) as source, bind_progress(events.append):
        report=run_pipeline(RunConfig(sources=[],output_dir=str(tmp_path/'output'),scopes=['A','B','public']),
            [source],rules=rules)
    assert attempted and not report.rules_published
    assert not any(e['code']=='publication_completed' for e in events)
    issues=[e for e in events if e['stage']=='publishing' and e['code']=='issue_detected']
    assert issues and all(e['counters']['published_rules']==0 for e in issues)
    assert issues[-1]['completed']==issues[-1]['total']
