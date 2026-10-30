"""Knowledge pagination is local; the issued evidence registry is cumulative."""
import json

from lightgraph.contracts import ExtractionGoal
from lightgraph.discovery.budget import BudgetAllocator
from lightgraph.discovery.tools import ToolExecutor
from lightgraph.documents import DocumentCorpus
from lightgraph.sources.memory import MemorySource
from lightgraph.tests.test_call_context import document, table


def make_tools(documents):
    tables=[table('a'),table('b')]
    adapter=MemorySource(tables,{t.qualified_name:[('K','caption')] for t in tables})
    tools=ToolExecutor(adapter,ExtractionGoal(),BudgetAllocator(),
        documents=DocumentCorpus(documents).bind_catalog(tables))
    return adapter,tools,tables


def test_table_lookup_omits_other_activated_tables_but_preserves_issued_refs():
    adapter,tools,tables=make_tools([document('global','Common definition.'),
        document('A','A-only definition.',binding='s:public.a'),
        document('B','B-only definition.',binding='s:public.b')])
    try:
        tools.begin_context([tables[1]])
        before=tools.document_refs()
        result=tools.execute('search_knowledge',{'table':'s:public.a'})
        assert result['status']=='ok'
        sections=result['data']['sections']
        assert {s['doc_id'] for s in sections}=={'global','A'}
        assert all(s['sources']==[{'source_id':'s','schema':'public','table':'a'}] for s in sections)
        assert result['data']['next_page'] is None and result['coverage']['result_complete']
        assert {s['doc_id'] for s in tools.context_documents()}=={'global','A','B'}
        assert {r['doc_id'] for r in tools.document_refs()}=={'global','A','B'}
        assert tools.documents.validate_references(before,tables)
        assert adapter.stats.queries==0
    finally:
        tools.close();adapter.close()


def test_column_filter_stays_local_after_wider_context_was_activated():
    code=document('code-guide','Identifies the object.',binding='s:public.a')
    label=document('label-guide','Describes the caption.',binding='s:public.a')
    for doc,column in ((code,'code'),(label,'label')):
        doc.scope.columns=[column]
        for section in doc.sections:
            section.scope=doc.scope.model_copy(deep=True)
    adapter,tools,tables=make_tools([code,label])
    try:
        tools.begin_context([tables[0]])
        assert {r['doc_id'] for r in tools.document_refs()}=={'code-guide','label-guide'}
        result=tools.execute('search_knowledge',{'table':'s:public.a','columns':['code']})
        assert result['status']=='ok'
        assert {s['doc_id'] for s in result['data']['sections']}=={'code-guide'}
        assert {r['doc_id'] for r in tools.document_refs()}=={'code-guide','label-guide'}
        denied=tools.execute('search_knowledge',{'table':'s:public.a','columns':['missing']})
        assert denied['status']=='error' and denied['code']=='unauthorized_column'
        assert adapter.stats.queries==0
    finally:
        tools.close();adapter.close()


def test_pages_are_stable_across_other_table_visits_and_keep_partial_markers():
    documents=[document('A','条件甲。'*6500,binding='s:public.a'),
               document('B','条件乙。'*6000,binding='s:public.b')]
    adapter,tools,tables=make_tools(documents)
    try:
        first=tools.execute('search_knowledge',{'table':'s:public.a','page':0})
        assert first['status']=='ok' and first['data']['next_page']==1
        assert first['coverage']['truncated'] and not first['coverage']['result_complete']
        tools.execute('get_metadata',{'table':'s:public.b'})
        repeated=tools.execute('search_knowledge',{'table':'s:public.a','page':0})
        assert repeated['data']==first['data']
        sections=list(first['data']['sections']);page=first['data']['next_page']
        while page is not None:
            result=tools.execute('search_knowledge',{'table':'s:public.a','page':page})
            assert result['status']=='ok'
            sections.extend(result['data']['sections']);page=result['data']['next_page']
        assert all(s['doc_id']=='A' and s['read_state']=='partial' for s in sections)
        assert len({s['locator'] for s in sections})==len(sections)
        assert ''.join(s['text'] for s in sections)==''.join(s.text for s in documents[0].sections)
        assert {r['doc_id'] for r in tools.document_refs()}=={'A','B'}
        assert '条件甲' not in json.dumps(tools.document_refs(),ensure_ascii=False)
        denied=tools.execute('search_knowledge',{'table':'s:public.a','page':len(sections)})
        assert denied['status']=='error' and denied['code']=='invalid_tool_arguments'
        assert adapter.stats.queries==0
    finally:
        tools.close();adapter.close()
