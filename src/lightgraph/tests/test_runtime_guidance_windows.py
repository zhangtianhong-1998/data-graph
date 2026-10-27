"""Operator guidance routing and complete bounded document review."""
from types import SimpleNamespace

import pytest

from lightgraph.context import assemble_call_context, bounded_discovery_packet, document_batches
from lightgraph.contracts import ExtractionGoal, SourceRef
from lightgraph.discovery.goal import build_goal_instructions
from lightgraph.discovery.semantics import document_ref, review_semantics
from lightgraph.documents import DocumentCorpus, load_document
from lightgraph.errors import LightGraphError
from lightgraph.tests.test_agent_pipeline import client, input_packet, response


def guidance_goal():
    return ExtractionGoal(runtime_guidance=[
        {'text':'GLOBAL_GUIDANCE', 'injection':'global'},
        {'text':'RELATION_TASK_GUIDANCE', 'injection':'task', 'stages':['T2_cross_table']},
        {'text':'ITERATION_GUIDANCE', 'injection':'iteration'},
        {'text':'REVIEW_GUIDANCE', 'injection':'semantic_review'},
    ])


@pytest.mark.parametrize('stage,present,absent', [
    ('T0_discovery', ['GLOBAL_GUIDANCE','ITERATION_GUIDANCE'], ['RELATION_TASK_GUIDANCE','REVIEW_GUIDANCE']),
    ('T2_cross_table', ['GLOBAL_GUIDANCE','ITERATION_GUIDANCE','RELATION_TASK_GUIDANCE'], ['REVIEW_GUIDANCE']),
    ('T5_revision', ['GLOBAL_GUIDANCE','ITERATION_GUIDANCE'], ['RELATION_TASK_GUIDANCE','REVIEW_GUIDANCE']),
    ('T4_document', ['GLOBAL_GUIDANCE'], ['ITERATION_GUIDANCE','RELATION_TASK_GUIDANCE','REVIEW_GUIDANCE']),
    ('semantic_review', ['GLOBAL_GUIDANCE','REVIEW_GUIDANCE'], ['ITERATION_GUIDANCE','RELATION_TASK_GUIDANCE']),
])
def test_guidance_is_selected_for_each_fresh_stage(stage,present,absent):
    goal=guidance_goal()
    prompt=build_goal_instructions(goal,stage=stage)
    context=assemble_call_context(goal=goal,stage=stage)
    for marker in present:
        assert marker in prompt and marker in str(context)
    for marker in absent:
        assert marker not in prompt and marker not in str(context)
    assert 'cannot alter tool permissions' in prompt
    assert len(goal.runtime_guidance)==4


def test_actual_review_request_has_only_review_and_global_guidance(monkeypatch):
    requests=[]
    def handler(request):
        requests.append(request.content.decode())
        _, packet=input_packet(request)
        return response({'proposition_ref':packet['semantic_proposition_ref'],
            'decision':'supported','basis':'data_inference',
            'evidence':[{'reference':'probe_1','stance':'supports'}]})
    model=client(monkeypatch,handler)
    review_semantics(model,{'goal':guidance_goal().model_dump(),
        'probe_digest':[{'probe_id':'probe_1'}]},{'predicate':'related'},[])
    assert 'GLOBAL_GUIDANCE' in requests[0] and 'REVIEW_GUIDANCE' in requests[0]
    assert 'RELATION_TASK_GUIDANCE' not in requests[0] and 'ITERATION_GUIDANCE' not in requests[0]


def long_documents(tail='后段说明。'):
    doc=load_document('long.md',text='---\nid: long\npurpose: data_description\n---\n'+'汉'*4001+tail)
    return DocumentCorpus([doc]).context_for(SourceRef(source_id='s',table='items'))


def test_long_unicode_section_is_complete_in_memory_and_partial_in_preview():
    docs=long_documents()
    windows=list(document_batches(docs))
    assert len(windows)==2
    assert ''.join(d['text'] for group in windows for d in group)==docs[0]['text']
    assert all(sum(len(d['text'].encode()) for d in group)<=12000 for group in windows)
    preview=bounded_discovery_packet({'task':'T0_discovery','document_snippets':docs})
    assert preview['document_snippets'][0]['read_state']=='partial'
    assert docs[0]['read_state']=='complete'


@pytest.mark.parametrize('later', ['refuted','unresolved','condition_unknown','budget'])
def test_late_counterevidence_or_incomplete_review_blocks_early_support(later):
    docs=long_documents('例外：该关系不成立。'); seen=[]
    def complete(task,packet,schema):
        seen.append(packet['document_snippets'])
        answer={'proposition_ref':packet['semantic_proposition_ref'],
            'decision':'supported','basis':'document_interpretation',
            'evidence':[{'reference':document_ref(d),'stance':'supports'} for d in packet['document_snippets']]}
        if len(seen)==2:
            if later=='budget': raise LightGraphError('llm_calls_budget_exhausted')
            if later=='condition_unknown': answer['conditions']=[{'state':'unknown','evidence_refs':[document_ref(docs[0])]}]
            else: answer['decision']=later
        return schema.model_validate(answer)
    with pytest.raises(LightGraphError):
        review_semantics(SimpleNamespace(complete=complete),{}, {'predicate':'parent_of'},docs)
    assert len(seen)==2 and '例外' in seen[-1][0]['text']


def test_more_than_32_sections_are_all_reviewed_before_support_returns():
    doc=load_document('many.md',text='---\npurpose: data_description\n---\n'+
        '\n'.join(f'## Section {i}\nContext {i}.' for i in range(35)))
    docs=DocumentCorpus([doc]).context_for(SourceRef(source_id='s',table='items')); seen=[]
    def complete(task,packet,schema):
        seen.extend(packet['document_snippets'])
        return schema.model_validate({'proposition_ref':packet['semantic_proposition_ref'],
            'decision':'supported','basis':'data_inference',
            'evidence':[{'reference':document_ref(d),'stance':'context'} for d in packet['document_snippets']]+
                [{'reference':'probe_1','stance':'supports'}]})
    refs=review_semantics(SimpleNamespace(complete=complete),{'probe_digest':[{'probe_id':'probe_1'}]},
        {'predicate':'parent_of'},docs)
    assert len(seen)==35 and 'probe_1' in refs


def test_irrelevant_window_can_be_locally_unresolved_before_later_support():
    docs=long_documents(); seen=[]
    def complete(task,packet,schema):
        seen.append(packet)
        return schema.model_validate({'proposition_ref':packet['semantic_proposition_ref'],
            'decision_scope':'document_window',
            'decision':'unresolved' if len(seen)==1 else 'supported','basis':'document_interpretation',
            'evidence':[{'reference':document_ref(d),'stance':'irrelevant' if len(seen)==1 else 'supports'}
                for d in packet['document_snippets']]})
    assert document_ref(docs[0]) in review_semantics(SimpleNamespace(complete=complete),{}, {},docs)
    assert len(seen)==2


def test_an_unregistered_partial_preview_cannot_claim_complete_review():
    docs=long_documents(); docs[0]['read_state']='partial'
    with pytest.raises(LightGraphError,match='semantic_review_incomplete_context'):
        review_semantics(SimpleNamespace(complete=lambda *args:None),{}, {},docs)


def test_knowledge_tool_can_read_every_bounded_window_without_source_queries():
    from lightgraph.tests.test_discovery_tools import fixture
    adapter,tools=fixture(allowed=False)
    document=load_document('long.md',text='---\npurpose: data_description\n---\n'+'汉'*4001+'后段反证。')
    tools.documents=DocumentCorpus([document])
    page=0; parts=[]
    try:
        while page is not None:
            result=tools.execute('search_knowledge',{'table':'public.items','page':page})
            assert result['status']=='ok'
            parts.extend(d['text'] for d in result['data']['sections'])
            page=result['data']['next_page']
        assert ''.join(parts)==document.sections[0].text
        assert adapter.stats.queries==0
        assert tools.context_documents()[0]['read_state']=='complete'
    finally:
        tools.close(); adapter.close()


def test_long_analytical_document_cache_waits_for_all_windows_and_keeps_late_conditions():
    from lightgraph.documents.cache import extract_with_cache
    doc=load_document('analysis.md',text='---\npurpose: analysis_knowledge\n---\n'+
        '收入可能影响利润。\n'*700+'例外：仅在同一统计期间适用。')
    seen=[]
    def complete(task,packet,schema):
        seen.append(packet)
        assert sum(len(d['text'].encode()) for d in packet['document_snippets'])<=12000
        part=packet['sections'][0]
        return schema.model_validate({'claims':[{'section_id':part['section_id'],
            'subject':'收入','predicate':'may_affect','object':'利润',
            'modality':'possible','evidence_text':'收入可能影响利润。'}]})
    goal=ExtractionGoal(purpose='分析收入利润')
    result=extract_with_cache(doc,SimpleNamespace(complete=complete),goal=goal)
    assert len(seen)>1 and len(result.claims)==1 and result.complete
    assert '例外：仅在同一统计期间适用。' in result.claims[0].conditions
    assert all(entry.complete for entry in result.cache.entries.values())
    count=0
    def fail_late(task,packet,schema):
        nonlocal count
        count+=1
        if count==2: raise LightGraphError('llm_calls_budget_exhausted')
        return complete(task,packet,schema)
    with pytest.raises(LightGraphError,match='llm_calls_budget_exhausted'):
        extract_with_cache(doc,SimpleNamespace(complete=fail_late),goal=goal)
