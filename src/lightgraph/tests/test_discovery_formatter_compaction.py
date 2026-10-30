"""Lossless preview reuse preserves native tools and all distinct evidence."""
import asyncio
from copy import deepcopy
import json

from agentscope.message import Msg

from lightgraph.contracts import canonical
from lightgraph.discovery.agentscope_runtime import DiscoveryFormatter


def section(**updates):
    return {'doc_id':'definition','revision':'revision_1','section_id':'section_1',
        'locator':'section:1','purpose':'data_description','scope':{},
        'sources':[],'read_state':'complete','evidence_role':'definition',
        'text':'The source code identifies the object.',**updates}


def format_messages(result, documents, *, feedback=None):
    packet={'task':'T1_table_discovery','document_snippets':documents}
    messages=[Msg('system','System rules','system'),Msg('user','Start discovery','user'),
        Msg('assistant',[{'type':'tool_use','id':'provider_call','name':'search_knowledge',
            'input':{'table':'s:public.items'}}],'assistant'),
        Msg('tool',[{'type':'tool_result','id':'provider_call','name':'search_knowledge',
            'output':[{'type':'text','text':canonical(result)}]}],'system')]
    if feedback is not None:
        messages.append(Msg('user',feedback,'user'))
    original=[deepcopy(message.content) for message in messages]
    output=asyncio.run(DiscoveryFormatter(packet,object()).format(messages))
    assert [message.content for message in messages]==original
    return output


def test_exact_preview_duplicates_reuse_task_content_and_preserve_tool_pairs():
    document=section()
    result={'status':'ok','probe_id':'probe_0001','context':{
        'documents':[deepcopy(document)],'document_refs':[{'doc_id':'definition'}],
        'tables':[{'table':'items'}]},'data':{'rows':[['source value']]}}
    output=format_messages(result,[document])
    compact=json.loads(output[-1]['content'])
    assert compact['context']['documents']==[]
    assert compact['context']['documents_reused_from_input']==['input.document_snippets[0]']
    assert compact['context']['document_refs']==result['context']['document_refs']
    assert compact['data']==result['data']
    assert json.loads(output[1]['content'])['input']['document_snippets']==[document]
    assert output[-1]['role']=='tool'
    assert output[-1]['tool_call_id']==output[-2]['tool_calls'][0]['id']=='provider_call'
    assert result['context']['documents']==[document]


def test_distinct_revisions_partial_sections_and_new_search_pages_are_preserved():
    visible=section()
    different=[section(revision='revision_2'),section(read_state='partial'),
        section(locator='section:1;chars:0-9',text='The source'),
        section(text='The source code identifies a different object.')]
    next_page=section(section_id='section_2',locator='section:2',text='A new page condition.')
    result={'status':'ok','context':{'documents':[visible,*different]},
        'data':{'sections':[visible,next_page],'page':1,'next_page':2},
        'coverage':{'result_complete':False,'truncated':True}}
    compact=json.loads(format_messages(result,[visible])[-1]['content'])
    assert compact['context']['documents']==different
    assert compact['context']['documents_reused_from_input']==['input.document_snippets[0]']
    assert compact['data']==result['data']
    assert compact['coverage']==result['coverage']


def test_nonmatching_context_stays_byte_identical_and_later_user_feedback_is_preserved():
    result={'status':'ok','context':{'documents':[section(revision='other')]}}
    feedback='The last proposal field is invalid. Keep the earlier drafts.'
    output=format_messages(result,[section()],feedback=feedback)
    assert output[-2]['content']==canonical(result)
    assert output[-1]=={'role':'user','content':feedback}
    assert sum('"output_schema"' in (msg.get('content') or '') for msg in output)==1


def test_tool_text_and_non_document_json_are_untouched():
    for content in ('not JSON','[1,2,3]','{"status":"ok","context":[]}'):
        assert DiscoveryFormatter._deduplicate_tool_context(content,[section()])==content
