"""Missing hierarchy endpoints defer their own work, not independent tables."""
from types import SimpleNamespace

from lightgraph.discovery.models import DiscoveryResult
from lightgraph.pipeline import _missing_hierarchy_foundation, run_pipeline
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import table
from lightgraph.tests.test_resume_pipeline import entity, isolate_discovery, model, result_for


def remembered():
    return {'object_id':'path','status':'candidate','basis':'agent_field_model',
        'positions':[{'position':i,'code':f'code_{i}','label':f'label_{i}'} for i in (1,2)]}


def test_only_missing_local_foundations_wait_and_ready_endpoint_releases_them():
    current=table('paths',['code','name'])
    knowledge=SimpleNamespace(current_objects=lambda t:[remembered()])
    assert _missing_hierarchy_foundation(current,[entity('other','other')],knowledge)
    assert not _missing_hierarchy_foundation(current,[entity('path','paths')],knowledge)
    knowledge.current_objects=lambda t:[]
    assert not _missing_hierarchy_foundation(current,[],knowledge)


def test_pipeline_does_not_spend_relation_calls_on_missing_path_endpoint(tmp_path,monkeypatch):
    tables=[table(name,['code','name']) for name in ('a','b','paths')]
    calls=[]
    def references(*args,**kwargs):
        calls.extend(kwargs['table_refs'])
        return DiscoveryResult(relation_table_outcomes={n:'resolved' for n in kwargs['table_refs']})
    isolate_discovery(monkeypatch,lambda *a,**kw:result_for([entity('a','a'),entity('b','b')]),references)
    monkeypatch.setattr('lightgraph.discovery.schema_knowledge.SchemaKnowledgeLedger.current_objects',
        lambda self,t:[remembered()] if t.name=='paths' else [])
    output=tmp_path/'out'
    with MemorySource(tables,{t.qualified_name:[(t.name,t.name)] for t in tables}) as source:
        report=run_pipeline(RunConfig(sources=[],output_dir=str(output)),[source],model=model())
    assert 'public.paths' not in calls
    assert set(calls)=={'public.a','public.b'}
    assert set(report.rules_published)=={'a','b'}
    assert any(i['code']=='relation_waiting_for_foundation' and i['table']=='public.paths' for i in report.issues)
    with GraphStore(output/'graph.sqlite') as store:
        rows=store.connection.execute("SELECT state,attempts FROM iteration_work WHERE task='relation_discovery' AND source_ref LIKE '%paths%'").fetchall()
        assert rows and all(row['state']=='deferred' and row['attempts']==0 for row in rows)
