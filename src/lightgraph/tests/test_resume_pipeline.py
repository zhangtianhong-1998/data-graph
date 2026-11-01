"""Resume partial discovery without withdrawing validated independent work."""
from types import SimpleNamespace

from lightgraph.contracts import RuleSpec, SourceRef
from lightgraph.discovery.budget import BudgetAllocator, Cost
from lightgraph.discovery.models import DataUnderstanding, DiscoveryResult
from lightgraph.pipeline import run_pipeline
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import table


def entity(rule_id, name, *, label='name', kind='Metric', domain=''):
    return RuleSpec(rule_id=rule_id, kind='column_group_entity',
        source={'source_id':'s','table':name},
        identity={'key_columns':['code'],'entity_kind':kind,'identity_domain':domain},
        attributes={'single_valued':{'label':label} if label else {}})


def result_for(rules, issues=()):
    return DiscoveryResult(rules=rules, issues=list(issues), understandings=[
        DataUnderstanding(source=r.source, dependency_hash='offline', columns=[], rule_refs=[r.rule_id])
        for r in rules])


def model():
    return SimpleNamespace(budget=BudgetAllocator(Cost(seconds=7200,llm_calls=240,llm_tokens=None)),
        usage={'calls':0}, explore=lambda *a,**kw:None)


def isolate_discovery(monkeypatch, discover, references=None):
    monkeypatch.setattr('lightgraph.discovery.discover',discover)
    monkeypatch.setattr('lightgraph.discovery.coverage.discover_uncovered',lambda **kw:([],{},[]))
    monkeypatch.setattr('lightgraph.discovery.discover_references',references or (
        lambda *a,**kw: DiscoveryResult(relation_table_outcomes={
            name:'resolved' for name in kw['table_refs']})))


def test_workflow_upgrade_keeps_published_rules_and_only_discovers_missing_table(tmp_path,monkeypatch):
    tables=[table('ready',['code','name']),table('missing',['code','name'])]
    calls=[]
    def discovery(*args,**kwargs):
        selected=kwargs['table_refs'];calls.append(set(selected))
        return result_for([entity('ready','ready')] if len(calls)==1 else [entity('missing','missing')],
            [{'table':'public.missing','code':'model_response_truncated','status':'deferred'}] if len(calls)==1 else [])
    isolate_discovery(monkeypatch,discovery)
    config=RunConfig(sources=[],output_dir=str(tmp_path/'out'))
    rows={'public.ready':[('A','Ready')],'public.missing':[('B','Missing')]}
    with MemorySource(tables,rows) as source:
        first=run_pipeline(config,[source],model=model())
        assert first.rules_published==['ready']
        monkeypatch.setattr('lightgraph.pipeline.DISCOVERY_WORKFLOW_VERSION','test-new-workflow')
        second=run_pipeline(config,[source],model=model())
    assert calls==[{'public.ready','public.missing'},{'public.missing'}]
    assert set(second.rules_published)=={'ready','missing'}
    assert second.counts['reused_rules']==1
    with GraphStore(tmp_path/'out/graph.sqlite') as store:
        assert {r.rule_id:r.revision for r in store.get_rules()}=={'ready':1,'missing':1}
        assert len([n for n in store.view({'public'})['nodes'] if n['layer']=='business'])==2


def test_failed_requested_retry_does_not_drop_existing_rule_from_same_table(tmp_path,monkeypatch):
    tables=[table('ready',['code','name'])];calls=[]
    def discovery(*args,**kwargs):
        calls.append(kwargs['table_refs'])
        return result_for([entity('ready','ready')]) if len(calls)==1 else DiscoveryResult(
            issues=[{'table':'public.ready','code':'model_response_truncated','status':'deferred'}])
    isolate_discovery(monkeypatch,discovery)
    config=RunConfig(sources=[],output_dir=str(tmp_path/'out'))
    with MemorySource(tables,{'public.ready':[('A','Ready')]}) as source:
        run_pipeline(config,[source],model=model())
        monkeypatch.setattr('lightgraph.runtime.candidates.CandidateQueue.retry_sources',
            lambda self:[SourceRef(source_id='s',table='ready')])
        report=run_pipeline(config,[source],model=model())
    assert len(calls)==2
    assert report.rules_published==['ready']
    assert report.counts['business_nodes']==1


def test_same_column_names_and_domain_do_not_delete_an_independent_projection(tmp_path):
    tables=[table('definitions',['code','name']),table('local_codes',['code','unit'])]
    defined=entity('defined','definitions',domain='metrics')
    local=entity('local','local_codes',label=None,domain='metrics')
    local.attributes.single_valued={'unit':'unit'}
    with MemorySource(tables,{'public.definitions':[('K1','Known')],
            'public.local_codes':[('K2','USD')]}) as source:
        report=run_pipeline(RunConfig(sources=[],output_dir=str(tmp_path/'out')),[source],rules=[defined,local])
    assert set(report.rules_published)=={'defined','local'}
    with GraphStore(tmp_path/'out/graph.sqlite') as store:
        nodes=[n for n in store.view({'public'})['nodes'] if n['layer']=='business']
        assert {n['properties']['business_code'] for n in nodes}=={'K1','K2'}
        assert next(n for n in nodes if n['properties']['business_code']=='K2')['properties']['unit']=='USD'


def test_relation_retry_sees_the_newly_validated_source_object(tmp_path,monkeypatch):
    tables=[table(name,['code','name']) for name in ('a','b','mapping')]
    initial=[entity('a','a'),entity('b','b',kind='Measure')]
    calls=[]
    def discovery(*args,**kwargs):
        return result_for([entity('mapping','mapping',kind='ReportItem')]
            if kwargs.get('table_refs')=={'public.mapping'} else [r.model_copy(deep=True) for r in initial])
    def references(adapter,endpoints,**kwargs):
        name=next(iter(kwargs['table_refs']));ids={r.rule_id for r in endpoints}
        if name=='public.mapping':
            calls.append(ids)
            if 'mapping' not in ids:
                return DiscoveryResult(issues=[{'table':name,'code':'reference_source_object_missing',
                    'status':'deferred'}],relation_table_outcomes={name:'deferred'})
        return DiscoveryResult(relation_table_outcomes={name:'resolved'})
    isolate_discovery(monkeypatch,discovery,references)
    rows={t.qualified_name:[(t.name,t.name)] for t in tables}
    with MemorySource(tables,rows) as source:
        report=run_pipeline(RunConfig(sources=[],output_dir=str(tmp_path/'out')),[source],model=model())
    assert calls==[{'a','b'},{'a','b','mapping'}]
    assert set(report.rules_published)=={'a','b','mapping'}
    assert not any(i['code']=='relation_discovery_pending' and i.get('table')=='public.mapping' for i in report.issues)
    assert not any(i['code']=='reference_source_object_missing' for i in report.issues)


def test_manual_rule_is_not_mutated_to_project_an_excluded_name(tmp_path):
    current=table('items',['code','name','unit']);rule=entity('items','items',label=None)
    rule.attributes.single_valued={'unit':'unit'}
    rule.attributes.excluded=[{'column':'name','reason':'explicitly excluded'}]
    rule=RuleSpec.model_validate(rule.model_dump())
    with MemorySource([current],{'public.items':[('K1','Do not project','USD')]}) as source:
        report=run_pipeline(RunConfig(sources=[],output_dir=str(tmp_path/'out')),[source],rules=[rule])
    assert report.rules_published==['items']
    assert 'label' not in rule.attributes.single_valued
