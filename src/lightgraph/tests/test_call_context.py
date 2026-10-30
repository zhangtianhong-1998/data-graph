"""Goal, scope and issued evidence stay aligned through actual tool/model turns."""
import json
from types import SimpleNamespace

import httpx
import pytest

from lightgraph.context import context_dependency_fingerprint
from lightgraph.contracts import ColumnSpec, ExtractionGoal, SourceRef, TableSpec
from lightgraph.discovery.budget import BudgetAllocator, Cost
from lightgraph.discovery.llm import ModelConfig, StructuredModelClient
from lightgraph.discovery.tools import ToolExecutor
from lightgraph.documents import DocumentCorpus, load_document, extract_with_cache, ClaimBatch, ClaimProposal
from lightgraph.errors import LightGraphError
from lightgraph.sources.memory import MemorySource


def table(name,source='s'):
    return TableSpec(source_id=source,name=name,columns=[ColumnSpec(name='code'),ColumnSpec(name='label')])


def document(name,body,*,binding=None,purpose='data_description'):
    scope='' if binding is None else f'applies_to: ["{binding}"]\n'
    return load_document(name+'.md',text=f'---\nid: {name}\npurpose: {purpose}\n{scope}---\n# Guide\n{body}')


def test_global_context_has_no_keyword_gate_and_source_binding_is_exact():
    a,b=table('same','A'),table('same','B')
    docs=DocumentCorpus([
        document('global','层级深度取决于当前分析入口。'),
        document('only_a','A_ONLY_DESCRIPTION',binding='A:public.same'),
        document('only_b','B_ONLY_DESCRIPTION',binding='B.public.same'),
        document('guidance','可以考虑检查异常。',purpose='analysis_knowledge'),
    ]).bind_catalog([a,b])
    selected=docs.contexts_for([a])
    assert {s['doc_id'] for s in selected}=={'global','only_a','guidance'}
    assert next(s for s in selected if s['doc_id']=='guidance')['evidence_role']=='exploration_guidance'
    assert all(s['sources']==[{'source_id':'A','schema':'public','table':'same'}] for s in selected)
    assert {s['doc_id'] for s in docs.contexts_for([a,b])}=={'global','only_a','only_b','guidance'}


def test_unclassified_global_document_is_visible_as_guidance_not_confirmation():
    doc=load_document('global.md',text='# 通用经验\n层级深度取决于当前入口。')
    assert doc.purpose=='unresolved'
    selected=DocumentCorpus([doc]).context_for(SourceRef(source_id='s',table='opaque'),['code'])
    assert len(selected)==1 and selected[0]['evidence_role']=='exploration_guidance'


@pytest.mark.parametrize('binding,code',[
    ('public.same','ambiguous_document_table_binding'),
    ('A:public.missing','invalid_document_table_binding'),
])
def test_invalid_or_ambiguous_binding_never_becomes_global(binding,code):
    with pytest.raises(LightGraphError,match=code):
        DocumentCorpus([document('bound','正文不含表名',binding=binding)]).bind_catalog(
            [table('same','A'),table('same','B')])


def test_source_and_column_binding_validation():
    doc=document('bound','限制用途')
    doc.scope.source_ids=['missing']
    with pytest.raises(LightGraphError,match='invalid_document_source_binding'):
        DocumentCorpus([doc]).bind_catalog([table('a')])
    doc.scope.source_ids=['s'];doc.scope.columns=['private_unknown']
    with pytest.raises(LightGraphError,match='invalid_document_column_binding'):
        DocumentCorpus([doc]).bind_catalog([table('a')])


def test_context_table_argument_cannot_restore_goal_excluded_columns():
    source=table('a')
    adapter=MemorySource([source],{'public.a':[('K','LABEL')]})
    doc=document('label-description','ONLY_LABEL_DESCRIPTION')
    doc.scope.columns=['label']
    for section in doc.sections:
        section.scope=doc.scope.model_copy(deep=True)
    tools=ToolExecutor(adapter,ExtractionGoal(excluded_columns=['label']),BudgetAllocator(),documents=[doc])
    tools.begin_context([source])
    assert tools.context_documents()==[]
    tools.close();adapter.close()


def test_dependency_set_detects_additions_removal_goal_and_binding_changes_only_where_applicable():
    a,b=table('a'),table('b')
    corpus=DocumentCorpus([document('global','只记录经过核验的关系。')]).bind_catalog([a,b])
    original=context_dependency_fingerprint(corpus,[a])
    corpus.documents.append(document('b','乙表说明',binding='s:public.b'))
    assert context_dependency_fingerprint(corpus,[a])==original
    corpus.documents.append(document('a','甲表说明',binding='s:public.a'))
    added=context_dependency_fingerprint(corpus,[a])
    assert added!=original
    assert context_dependency_fingerprint(corpus,[a],ExtractionGoal(purpose='组织图'))!=added
    corpus.documents[-1].scope.tables=['s:public.b']
    assert context_dependency_fingerprint(corpus,[a])==original
    corpus.documents.pop(0)
    assert context_dependency_fingerprint(corpus,[a])!=original


def test_complete_document_conditions_are_not_silently_dropped_at_budget_boundary():
    corpus=DocumentCorpus([document('global','首段定义。\n## 例外\n仅在同一体系内成立。')])
    from lightgraph.context import document_batches
    sections=corpus.context_for(SourceRef(source_id='s',table='a'),['code'],max_bytes=20)
    windows=list(document_batches(sections,max_bytes=20))
    assert len(windows)>1
    assert ''.join(d['text'] for batch in windows for d in batch)==''.join(d['text'] for d in sections)
    assert any('仅在' in d['text'] for batch in windows for d in batch)


def test_section_scope_and_purpose_inherit_and_can_only_narrow():
    doc=load_document('sections.md',text='''---
id: scoped-sections
purpose: mixed
sections:
  方法/甲表:
    purpose: analysis_knowledge
    scope:
      tables: [s:public.a]
---
# 方法
适用条件：同一统计期间。
## 甲表
成本可能影响利润。
### 子段
价格可能影响收入。
''')
    a,b=table('a'),table('b')
    corpus=DocumentCorpus([doc]).bind_catalog([a,b])
    selected=corpus.contexts_for([a])
    assert len(selected)==3
    assert all(s['evidence_role']=='exploration_guidance' for s in selected[1:])
    assert all(s['scope']['tables']==['s:public.a'] for s in selected[1:])
    assert len(corpus.contexts_for([b]))==1
    expanded=load_document('bad.md',text='''---
purpose: mixed
applies_to: [s:public.a]
sections:
  方法:
    scope:
      tables: [s:public.b]
---
# 方法
错误范围不能自动升级为全局。
''')
    with pytest.raises(LightGraphError,match='document_section_scope_expansion'):
        DocumentCorpus([expanded]).bind_catalog([a,b])


def test_cross_source_tool_preserves_both_scopes_and_never_offers_federated_sql():
    a,b=table('same','A'),table('same','B')
    left=MemorySource([a],{'public.same':[('K','left')]})
    right=MemorySource([b],{'public.same':[('K','right')]})
    corpus=DocumentCorpus([document('A','左端定义',binding='A:public.same'),
                           document('B','右端定义',binding='B:public.same')]).bind_catalog([a,b])
    tools=ToolExecutor(left,ExtractionGoal(),BudgetAllocator(),documents=corpus,allow_source_samples=True,
                       source_adapters={'A':left,'B':right})
    result=tools.execute('check_reference',{'source_table':'A:public.same','source_columns':['code'],
        'target_table':'B:public.same','target_columns':['code']})
    assert result['status']=='ok' and result['aggregates']['unique_matches']==1
    assert result['coverage']['confirmed'] and not result['coverage']['semantic_identity_confirmed']
    assert {r['doc_id'] for r in tools.document_refs()}=={'A','B'}
    assert tools.execute('search_knowledge',{'table':'public.same'})['code']=='unauthorized_object'
    before=left.stats.queries+right.stats.queries
    rejected=tools.execute('execute_readonly_sql',{'sql':'SELECT code FROM B.public.same'})
    assert rejected['status']=='error' and left.stats.queries+right.stats.queries==before
    tools.close();left.close();right.close()


def fixture_tools():
    tables=[table(n) for n in ('a','b','c')]
    adapter=MemorySource(tables,{t.qualified_name:[('K','OBSERVATION_CANARY_9271')] for t in tables})
    corpus=DocumentCorpus([document('global','定义须带条件。')]+[
        document(n.upper(),n.upper()+'_MARK',binding='s:public.'+n) for n in ('a','b','c')]).bind_catalog(tables)
    tools=ToolExecutor(adapter,ExtractionGoal(purpose='指标图'),BudgetAllocator(),documents=corpus,allow_source_samples=True)
    return adapter,tools,tables


def test_sql_ast_adds_actual_join_tables_before_rows_and_catalog_listing_does_not(monkeypatch):
    adapter,tools,tables=fixture_tools()
    tools.begin_context([tables[0]])
    tools.execute('get_metadata',{})
    assert {s['doc_id'] for s in tools.context_documents()}=={'global','A'}
    real_execute=adapter.execute
    def execute(*args,**kwargs):
        assert {s['doc_id'] for s in tools.context_documents()}=={'global','A','B'}
        yield from real_execute(*args,**kwargs)
    monkeypatch.setattr(adapter,'execute',execute)
    result=tools.execute('execute_readonly_sql',{'sql':(
        'WITH x AS (SELECT code, label FROM public.b) '
        'SELECT a.code, b.label FROM public.a a JOIN x b ON a.code=b.code')})
    assert result['status']=='ok' and result['data']['rows']==[['K','OBSERVATION_CANARY_9271']]
    refs=tools.document_refs()
    assert {r['doc_id'] for r in refs}=={'global','A','B'}
    assert 'OBSERVATION_CANARY' not in json.dumps(refs)
    tools.begin_context([tables[2]])
    assert {s['doc_id'] for s in tools.context_documents()}=={'global','C'}
    tools.close()
    assert {r['doc_id'] for r in tools.document_refs()}=={'global','C'}
    adapter.close()


def test_registered_revision_and_scope_revalidated_before_compilation():
    adapter,tools,tables=fixture_tools()
    tools.begin_context([tables[0]])
    tools.execute('search_knowledge',{'table':'s:public.b'})
    assert {d['doc_id'] for d in tools.context_documents()}=={'global','A','B'}
    tools.documents.documents[2]=document('B','修订后的定义',binding='s:public.b')
    with pytest.raises(LightGraphError,match='stale_document_reference'):
        tools.context_documents()
    tools.close();adapter.close()


def test_agent_registers_dynamic_sections_for_final_evidence_and_next_task_starts_clean(monkeypatch,tmp_path,capsys,caplog):
    monkeypatch.setenv('CONTEXT_TEST_KEY','synthetic')
    adapter,tools,tables=fixture_tools()
    turns=[]
    def transport(request):
        body=json.loads(request.content);turns.append(body)
        context=json.loads(body['messages'][1]['content'])['call_context']
        assert context['goal']['purpose']=='指标图'
        assert context['stage']=='T0_discovery' and context['subtask']
        if len(turns)==1:
            message={'content':None,'tool_calls':[{'id':'provider-id','type':'function','function':{
                'name':'execute_readonly_sql','arguments':json.dumps({'sql':'SELECT code, label FROM public.b'})}}]}
        else:
            assert {d['doc_id'] for d in context['document_directory']}=={'global','A','B'}
            assert 'OBSERVATION_CANARY_9271' in body['messages'][-1]['content']
            message={'content':json.dumps({'task':'T0_discovery','proposals':[{'kind':'column_group','claim':{
                'group_id':'g','entity_kind_hint':'Metric','members':[{'column_id':'c_code','role':'identity_code'}]},
                'evidence_cited':['B']}]})}
        return httpx.Response(200,json={'choices':[{'message':message,'finish_reason':'tool_calls' if message.get('tool_calls') else 'stop'}]})
    model=StructuredModelClient(ModelConfig(base_url='https://test.invalid',model='mock',api_key_env='CONTEXT_TEST_KEY',
        allow_source_samples=True),BudgetAllocator(Cost(llm_calls=8,llm_tokens=1000000,memory_bytes=1000000,seconds=60)),httpx.MockTransport(transport))
    packet={'task':'T0_discovery','goal':{'purpose':'指标图'},
        'scope':{'source_id':'s','schema':'public','table':'a'},
        'metadata_digest':{'columns':[{'id':'c_code','name':'code'}]}}
    result=model.explore(packet,tools)
    assert result.proposals[0].evidence_cited==['B']
    assert {r['doc_id'] for r in packet['document_refs']}=={'global','A','B'}
    assert {s['doc_id'] for s in tools.context_documents()}=={'global','A','B'}
    assert 'OBSERVATION_CANARY' not in json.dumps(packet)
    assert not list(tmp_path.iterdir()) and 'OBSERVATION_CANARY' not in caplog.text+capsys.readouterr().out
    # Reusing the model creates no conversation history or old table documents.
    def second(request):
        text=request.content.decode()
        assert 'A_MARK' not in text and 'B_MARK' not in text and 'OBSERVATION_CANARY' not in text
        body=json.loads(request.content)
        context=json.loads(body['messages'][1]['content'])['call_context']
        assert context['goal']['purpose']=='组织图'
        assert {d['doc_id'] for d in context['document_directory']}=={'global','C'}
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'task':'T0_discovery','abstain':True})},'finish_reason':'stop'}]})
    model.transport=httpx.MockTransport(second)
    next_tools=ToolExecutor(adapter,ExtractionGoal(purpose='组织图'),model.budget,documents=tools.documents)
    model.explore({'task':'T0_discovery','goal':{'purpose':'组织图'},'scope':{'source_id':'s','table':'c'}},next_tools)
    adapter.close()


def test_document_extraction_goal_changes_output_and_invalidates_only_context_cache():
    doc=document('knowledge','# 收入\n价格可能影响收入。\n# 利润\n成本可能影响利润。',purpose='analysis_knowledge')
    # Avoid the helper's heading-only preface: it is context rather than a claim.
    class Model:
        config=SimpleNamespace(model='goal-aware-mock')
        def __init__(self):self.calls=[]
        def complete(self,task,packet,response_model):
            self.calls.append(packet)
            assert task==packet['stage']==packet['call_context']['stage']=='T4_document'
            assert packet['call_context']['goal']==packet['goal']
            section=packet['sections'][0]
            wanted=packet['goal']['purpose']
            if wanted not in section['text']:
                return ClaimBatch(out_of_goal=True)
            subject='价格' if wanted=='收入' else '成本'
            return ClaimBatch(claims=[ClaimProposal(section_id=section['section_id'],subject=subject,
                predicate='may_affect',object=wanted,modality='possible',evidence_text=f'{subject}可能影响{wanted}')])
    model=Model()
    first=extract_with_cache(doc,model,goal=ExtractionGoal(purpose='收入'))
    assert [c.object for c in first.claims]==['收入']
    same=extract_with_cache(doc,model,goal=ExtractionGoal(purpose='收入'),cached_sections=first.cache)
    assert same.extracted_sections==0
    changed=extract_with_cache(doc,model,goal=ExtractionGoal(purpose='利润'),cached_sections=first.cache)
    assert changed.reused_sections==0 and [c.object for c in changed.claims]==['利润']
    subtask=extract_with_cache(doc,model,goal=ExtractionGoal(purpose='利润'),subtask='核对条件与例外',cached_sections=changed.cache)
    assert subtask.reused_sections==0
    assert 'call_context' not in changed.cache.model_dump_json() and '"text":' not in changed.cache.model_dump_json()


def test_single_turn_model_entry_always_has_goal_and_stage(monkeypatch):
    monkeypatch.setenv('CONTEXT_TEST_KEY','synthetic')
    def transport(request):
        body=json.loads(request.content)
        assert '组织图' in body['messages'][0]['content']
        packet=json.loads(body['messages'][1]['content'])['input']
        assert packet['goal']['purpose']=='组织图' and packet['call_context']['stage']=='T1_table_discovery'
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'task':'T1_table_discovery','abstain':True})},'finish_reason':'stop'}]})
    model=StructuredModelClient(ModelConfig(base_url='https://test.invalid',model='mock',api_key_env='CONTEXT_TEST_KEY'),
        transport=httpx.MockTransport(transport))
    assert model.propose({'task':'T1_table_discovery','goal':{'purpose':'组织图'}}).abstain
