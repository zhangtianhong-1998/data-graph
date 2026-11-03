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
