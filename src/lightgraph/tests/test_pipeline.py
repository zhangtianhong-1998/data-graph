"""End-to-end execution outcomes, separate from model and real-database acceptance."""
import json
from pathlib import Path
import pytest
from lightgraph.contracts import QueryLimits
from lightgraph.documents import load_document
from lightgraph.pipeline import run_pipeline
from lightgraph.runtime.config import RunConfig
from lightgraph.runtime.retention import retain_check
from lightgraph.sample import build_sample,sample_rules,DESCRIPTION,ANALYSIS,write_sample
from lightgraph.sources import MemorySource,ExcelSource
from lightgraph.store import GraphStore
from lightgraph.cli import main

CANARY='RETENTION_CANARY_9f2d17'

def docs():return [load_document('description.md',text=DESCRIPTION),load_document('analysis.md',text=ANALYSIS)]
def config(tmp_path,**kwargs):return RunConfig(sources=[],output_dir=str(tmp_path/'output'),scopes=['A','B','public'],**kwargs)
def execute(tmp_path,rows=None,rules=True,documents=True,**cfg):
    tables,data=build_sample(5000)
    with MemorySource(tables,rows if rows is not None else data) as source:
        return run_pipeline(config(tmp_path,**cfg),[source],rules=sample_rules() if rules else None,documents=docs() if documents else [])
def graph(tmp_path,scopes=None):
    with GraphStore(tmp_path/'output/graph.sqlite') as store:return store.view(set(scopes or ['A','B','public']),include_candidates=True)
def business(g):return [n for n in g['nodes'] if n['layer']=='business' and n['status']=='accepted']
def code_nodes(g,kind=None):
    return {n['properties']['business_code']:n for n in business(g) if 'business_code' in n['properties'] and isinstance(n['properties']['business_code'],str) and (kind is None or n['kind']==kind)}


def test_full_execution_duplicates_hierarchy_references_and_retention(tmp_path):
    report=execute(tmp_path)
    assert len(report.rules_published)==7 and not report.rules_skipped
    assert [i['code'] for i in report.issues]==['gap_in_path']
    g=graph(tmp_path);sales=[n for n in business(g) if n['label']=='销售费用']
    assert len(sales)==1
    assert set(sales[0]['aliases'])=={'Selling expense','Sales cost'}
    n=code_nodes(g);edges={(e['source'],e['target']) for e in g['edges'] if e['relation']=='parent_of'}
    assert (n['3']['id'],n['26']['id']) not in edges
    assert (n['271']['id'],n['2711']['id']) in edges
    assert (n['271']['id'],n['2713']['id']) in edges
    assert (n['2711']['id'],n['2713']['id']) not in edges
    assert all(a!=b for a,b in edges)
    assert len([e for e in g['edges'] if e['relation']=='uses_measure'])==1
    assert any(e['relation']=='verified_reference' for e in g['edges'])
    assert g['claims'] and all(c['status']=='candidate' for c in g['claims'])
    assert retain_check([tmp_path/'output'],[CANARY])['violations']==0
    assert report.counts['returned_rows']==16 # 5,000 source rows become 16 distinct useful tuples.
    assert all(report.validation[g]=='not_run' for g in ['E','D','I'])


def test_incremental_repetition_stable_identity_and_fact_set(tmp_path):
    execute(tmp_path);before=graph(tmp_path)
    execute(tmp_path);after=graph(tmp_path)
    assert {n['id'] for n in before['nodes']}=={n['id'] for n in after['nodes']}
    assert {f['id'] for f in before['facts']}=={f['id'] for f in after['facts']}


def test_shared_alias_withdrawal_preserves_other_scope(tmp_path):
    execute(tmp_path)
    initial=next(n for n in business(graph(tmp_path)) if n['label']=='销售费用')
    _,rows=build_sample(5000)
    rows['public.report_item']=[r for r in rows['public.report_item'] if r['tenant_id']=='B']
    result=execute(tmp_path,rows=rows)
    assert len(result.rules_published)==7
    remaining=next(n for n in business(graph(tmp_path)) if n['label']=='销售费用')
    assert remaining['id']==initial['id'] and remaining['aliases']==['Sales cost']
    assert not any(n['label']=='销售费用' for n in business(graph(tmp_path,['A','public'])))


def test_atomic_dependency_update_and_deletion(tmp_path):
    execute(tmp_path)
    _,rows=build_sample(5000)
    rows['public.measure_registry']=[];rows['public.metric_measure_map']=[]
    result=execute(tmp_path,rows=rows)
    assert len(result.rules_published)==7
    assert not [n for n in business(graph(tmp_path)) if n['kind']=='Measure']
    assert not [e for e in graph(tmp_path)['edges'] if e['relation']=='uses_measure']


def test_failed_reference_cannot_publish_changed_target(tmp_path):
    execute(tmp_path);before=graph(tmp_path)
    _,rows=build_sample(5000);rows['public.measure_registry']=[]
    result=execute(tmp_path,rows=rows)
    assert result.status=='PARTIAL' and not result.rules_published
    assert {f['id'] for f in before['facts']}=={f['id'] for f in graph(tmp_path)['facts']}


def test_small_budget_reports_partial_without_false_completion(tmp_path):
    result=execute(tmp_path,max_db_queries=1)
    assert result.status=='PARTIAL'
    assert any('budget' in i['code'] for i in result.issues)
    assert retain_check([tmp_path/'output'],[CANARY])['violations']==0


def test_document_partial_preserves_prior_claims(tmp_path):
    execute(tmp_path);before=graph(tmp_path)['claims']
    tables,rows=build_sample(100)
    changed=load_document('analysis.md',text=ANALYSIS+'\n# 新章节\n未定义的复杂分析需要探索，但当前规则不能说明其意义。\n')
    with MemorySource(tables,rows) as source:
        result=run_pipeline(config(tmp_path),[source],rules=sample_rules(),documents=[changed])
    assert any(i['code']=='document_extract_incomplete' for i in result.issues)
    assert {c['fact_id'] for c in before}=={c['fact_id'] for c in graph(tmp_path)['claims']}


def test_document_support_is_not_public_when_scope_private(tmp_path):
    result=execute(tmp_path,document_scopes=['A'])
    assert result.status=='COMPLETED_WITH_GAPS'
    assert graph(tmp_path,['A','public'])['claims']
    assert not graph(tmp_path,['B','public'])['claims']


def test_explicit_document_removal_retracts_its_claims(tmp_path):
    execute(tmp_path);docid=docs()[1].document_id
    result=execute(tmp_path,documents=False,removed_document_ids=[docid])
    assert not graph(tmp_path)['claims']


def test_excel_roundtrip_and_cli(tmp_path,capsys):
    source_dir=tmp_path/'input';write_sample(source_dir,500)
    from lightgraph.pipeline import load_rules
    with ExcelSource(source_dir/'input.xlsx',source_id='excel') as source:
        result=run_pipeline(config(tmp_path),[source],rules=load_rules([source_dir/'rules.yaml']),documents=docs())
    assert len(result.rules_published)==7
    g=graph(tmp_path)
    assert sum(n['kind']=='Workbook' for n in g['nodes'])==1
    assert sum(n['kind']=='Worksheet' for n in g['nodes'])==8
    assert main(['report',str(tmp_path/'output')])==0
    assert 'COMPLETED_WITH_GAPS' in capsys.readouterr().out
    assert main(['retain-check',str(tmp_path/'output'),'--marker',CANARY])==0


def test_discovery_can_reuse_rules_without_calling_model(tmp_path):
    result=execute(tmp_path,rules=False)
    assert result.rules_published
    class NeverCall:
        usage={'calls':0}
        def complete(self,*a,**k):raise AssertionError('unchanged dependencies must reuse')
        def propose(self,*a,**k):raise AssertionError('unchanged dependencies must reuse')
    tables,rows=build_sample(5000)
    with MemorySource(tables,rows) as source:
        again=run_pipeline(config(tmp_path),[source],documents=docs(),model=NeverCall())
    assert again.status in ('COMPLETED','COMPLETED_WITH_GAPS')
    assert again.counts['reused_rules']==len(result.rules_published)
    assert again.counts['llm_calls']==0


def test_discover_only_exports_valid_rules_without_graph(tmp_path):
    tables,rows=build_sample(200)
    with MemorySource(tables,rows) as source:
        result=run_pipeline(config(tmp_path),[source],documents=docs(),discover_only=True)
    assert result.rules_published
    assert (tmp_path/'output/rules.yaml').exists()
    assert not (tmp_path/'output/graph.sqlite').exists()


def test_document_alignment_does_not_block_source_deletion(tmp_path):
    execute(tmp_path,document_scopes=['A'])
    before=graph(tmp_path)
    assert any(e['relation']=='analyzes' for e in before['edges'])
    _,rows=build_sample(5000)
    for key in rows:rows[key]=[]
    result=execute(tmp_path,rows=rows,document_scopes=['A'])
    assert len(result.rules_published)==7
    g=graph(tmp_path)
    assert not business(g)
    assert g['claims']
    assert not any(e['relation']=='analyzes' for e in g['edges'])


def test_export_import_roundtrip_preserves_graph_and_scope(tmp_path):
    execute(tmp_path)
    from lightgraph.presentation.importer import import_graph
    report=import_graph(tmp_path/'output/graph.json',tmp_path/'import.sqlite')
    expected=graph(tmp_path)
    assert report['nodes']==len(expected['nodes'])
    assert report['edges']==len(expected['edges'])
    with GraphStore(tmp_path/'import.sqlite') as store:
        imported=store.view({'A','B','public'},include_candidates=True)
        assert {f['id'] for f in imported['facts']}=={f['id'] for f in expected['facts']}
        only_b=store.view({'B','public'},include_candidates=True)
        assert 'Selling expense' not in json.dumps(only_b,ensure_ascii=False)


def test_new_table_is_discovered_on_cached_source(tmp_path):
    execute(tmp_path,rules=False)
    from lightgraph.contracts import TableSpec,ColumnSpec
    tables,rows=build_sample(5000)
    tables.append(TableSpec(source_id='synthetic',name='another_metric',columns=[ColumnSpec(name=c) for c in ['metric_code','metric_name']]))
    rows['public.another_metric']=[{'metric_code':'NEW1','metric_name':'测试新增指标'}]
    with MemorySource(tables,rows) as source:
        result=run_pipeline(config(tmp_path),[source],documents=docs())
    assert any(n['label']=='测试新增指标' for n in business(graph(tmp_path))),result


def test_schema_change_removes_dependent_metadata_edges_before_catalog_replacement(tmp_path):
    execute(tmp_path,rules=False)
    tables,rows=build_sample(5000)
    table=next(t for t in tables if t.name=='product_dictionary')
    table.columns=[c for c in table.columns if c.name!='product_en_name']
    rows['public.product_dictionary']=[{k:v for k,v in r.items() if k!='product_en_name'} for r in rows['public.product_dictionary']]
    with MemorySource(tables,rows) as source:
        result=run_pipeline(config(tmp_path),[source],documents=docs())
    assert result.status in ('COMPLETED','COMPLETED_WITH_GAPS')
    assert not any(n['kind']=='Column' and n['label']=='product_en_name' for n in graph(tmp_path)['nodes'])


def test_export_schema_and_repeat_bytes(tmp_path):
    execute(tmp_path)
    import jsonschema
    from lightgraph.presentation.schema import snapshot_schema
    from lightgraph.presentation import export_graph
    path=tmp_path/'output/graph.json'
    jsonschema.validate(json.loads(path.read_text()),snapshot_schema())
    original=path.read_bytes()
    with GraphStore(tmp_path/'output/graph.sqlite') as store:
        export_graph(store,tmp_path/'output',scopes={'A','B','public'})
    assert path.read_bytes()==original


def test_pipeline_partition_supports_and_withdrawal(tmp_path):
    rules=sample_rules()
    for r in rules:r.scope.partition_by=['tenant_id']
    tables,rows=build_sample(5000)
    with MemorySource(tables,rows) as source:
        result=run_pipeline(config(tmp_path),[source],rules=rules,documents=docs())
    assert len(result.rules_published)==7,result
    before=next(n for n in business(graph(tmp_path)) if n['label']=='销售费用')
    rows['public.report_item']=[r for r in rows['public.report_item'] if r['tenant_id']=='B']
    with MemorySource(tables,rows) as source:
        result=run_pipeline(config(tmp_path),[source],rules=rules,documents=docs())
    assert len(result.rules_published)==7,result
    after=next(n for n in business(graph(tmp_path)) if n['label']=='销售费用')
    assert before['id']==after['id'] and after['aliases']==['Sales cost']


def test_published_document_cache_reuses_siblings_and_failure_preserves_cache(tmp_path):
    from types import SimpleNamespace
    from lightgraph.pipeline import publish_documents
    from lightgraph.documents import ClaimBatch,ClaimProposal
    class Model:
        config=SimpleNamespace(model='mock')
        def __init__(self):self.calls=0;self.fail=False
        def complete(self,task,packet,response_model):
            self.calls+=1
            if self.fail:return ClaimBatch(abstain=True)
            section=packet['sections'][0]
            for subject in ('价格','成本','费用'):
                quote=subject+'可能影响收入'
                if quote in section['text']:
                    return ClaimBatch(claims=[ClaimProposal(section_id=section['section_id'],subject=subject,
                        predicate='may_affect',object='收入',modality='possible',evidence_text=quote)])
            return ClaimBatch()
    text='---\nid: analysis-cache\npurpose: analysis_knowledge\n---\n# 价格\n价格可能影响收入。\n# 成本\n成本可能影响收入。\n'
    model=Model()
    with GraphStore(tmp_path/'cache.sqlite') as store:
        first=load_document('guide.md',text=text)
        count,issues=publish_documents(store,[first],'default',model)
        assert count==2 and not issues and model.calls==2
        initial=store.get_document_cache('document:analysis-cache')
        assert initial and 'evidence_text' not in initial.model_dump_json()
        changed=load_document('guide.md',text=text.replace('成本','费用'))
        count,issues=publish_documents(store,[changed],'default',model)
        assert count==2 and not issues and model.calls==3
        assert all(c['status']=='candidate' for c in store.view({'public'},include_candidates=True)['claims'])
        saved=store.get_document_cache('document:analysis-cache').model_dump_json()
        version=store.current_versions('document:analysis-cache')
        model.fail=True
        broken=load_document('guide.md',text=text.replace('价格','未知'))
        count,issues=publish_documents(store,[broken],'default',model)
        assert count==0 and issues[0]['code']=='document_extract_incomplete'
        assert store.get_document_cache('document:analysis-cache').model_dump_json()==saved
        assert store.current_versions('document:analysis-cache')==version
        # Explicit invalidation makes the cache unavailable, without deleting history.
        store.invalidate_rule('document:analysis-cache','explicit_document_removed')
        assert store.get_document_cache('document:analysis-cache') is None


def test_claim_cache_requires_typed_complete_published_projection():
    from lightgraph.documents import ClaimCache
    from lightgraph.errors import LightGraphError
    with GraphStore() as store:
        pub=store.begin_publication('document:d',1,'all',{'document':'d@1'})
        with pytest.raises(LightGraphError,match='current_publication'):
            store.put_document_cache(pub,ClaimCache())
        with pytest.raises(LightGraphError,match='typed_claim_cache_required'):
            store.put_document_cache(pub,{'raw_rows':[]})


def test_narrowed_extraction_goal_invalidates_previous_out_of_scope_supports(tmp_path):
    from lightgraph.contracts import ExtractionGoal
    execute(tmp_path)
    assert any(n['kind']=='Metric' for n in business(graph(tmp_path)))
    result=execute(tmp_path,goal=ExtractionGoal(source_scope=['region_dictionary']))
    assert result.status=='COMPLETED_WITH_GAPS'
    assert not any(n['kind']=='Metric' for n in business(graph(tmp_path)))
    assert not any(e['relation']=='uses_measure' for e in graph(tmp_path)['edges'])


def test_relation_goal_removes_previously_published_reference(tmp_path):
    from lightgraph.contracts import ExtractionGoal
    execute(tmp_path)
    result=execute(tmp_path,goal=ExtractionGoal(relation_kinds=['parent_of']))
    assert result.status=='COMPLETED_WITH_GAPS'
    assert not any(e['relation']=='uses_measure' for e in graph(tmp_path)['edges'])
    assert any(n['kind']=='Metric' for n in business(graph(tmp_path)))


def test_document_deadline_before_publication_preserves_prior_claims(tmp_path):
    from lightgraph.pipeline import publish_documents
    from lightgraph.errors import BudgetExceeded
    with GraphStore(tmp_path/'deadline.sqlite') as store:
        first=load_document('analysis.md',text=ANALYSIS)
        assert not publish_documents(store,[first],'default')[1]
        prior=store.current_versions('document:'+first.document_id)
        before={c['fact_id'] for c in store.view({'public'},include_candidates=True)['claims']}
        changed=load_document('analysis.md',text=ANALYSIS.replace('销售费用','管理费用'))
        ticks=[]
        def deadline():
            ticks.append(1)
            if len(ticks)>1:raise BudgetExceeded('run_time_budget_exhausted')
        count,issues=publish_documents(store,[changed],'default',check_time=deadline)
        assert not count and issues[0]['code']=='run_time_budget_exhausted'
        assert store.current_versions('document:'+first.document_id)==prior
        assert before=={c['fact_id'] for c in store.view({'public'},include_candidates=True)['claims']}


def test_whole_document_rechecks_extractor_dependency(tmp_path):
    from lightgraph.pipeline import publish_documents
    from lightgraph.documents import ClaimBatch,ClaimProposal
    from types import SimpleNamespace
    class Model:
        def __init__(self,name):self.config=SimpleNamespace(model=name);self.calls=0
        def complete(self,task,packet,response_model):
            self.calls+=1;s=packet['sections'][0]
            return ClaimBatch(claims=[ClaimProposal(section_id=s['section_id'],subject='价格',predicate='may_affect',
                object='收入',modality='possible',evidence_text='价格可能影响收入')])
    doc=load_document('guide.md',text='---\nid: model-change\npurpose: analysis_knowledge\n---\n# 价格\n价格可能影响收入。\n')
    with GraphStore(tmp_path/'cache.sqlite') as store:
        old=Model('one');new=Model('two')
        assert not publish_documents(store,[doc],'default',old)[1]
        assert not publish_documents(store,[doc],'default',old)[1]
        assert old.calls==1
        assert not publish_documents(store,[doc],'default',new)[1]
        assert new.calls==1


def test_cache_hit_does_not_silently_hide_unmodeled_table(tmp_path):
    first=execute(tmp_path,rules=False)
    again=execute(tmp_path,rules=False)
    for result in (first,again):
        assert any(i.get('table')=='public.observation_log' and i['code']=='no_confirmable_entity_group' for i in result.issues)
    assert again.counts['reused_rules']==7
    assert again.counts['llm_calls']==0
