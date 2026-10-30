"""Incremental model output preserves independent candidates and all guardrails."""
import json

import httpx
import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from lightgraph.discovery.models import ProposalBatch, TASK_PROPOSAL_KINDS
from lightgraph.discovery.proposal_session import (DiscoveryFinish, ProposalSession,
                                                   proposal_tool_specs)
from lightgraph.tests.test_discovery_agent import Toolbox, final, input_packet, response, CANARY
from lightgraph.tests.test_model_streaming import model
from lightgraph.tests.test_agentscope_runtime import framework_spy, assert_cleared


def group():
    return {key:value for key,value in final()['proposals'][0].items() if key!='kind'}


def tools_response(*operations):
    return httpx.Response(200,json={'choices':[{'message':{'content':None,'tool_calls':[
        {'id':f'call_{index}','type':'function','function':{'name':name,'arguments':json.dumps(args)}}
        for index,(name,args) in enumerate(operations)]},'finish_reason':'tool_calls'}],
        'usage':{'prompt_tokens':30,'completion_tokens':20}})


def test_individual_error_keeps_draft_and_definitions_are_append_only():
    session=ProposalSession(input_packet())
    first=session.execute('submit_column_group',group())
    assert first=={'status':'ok','draft_id':'draft_1','confirmed':False}
    assert session.execute('submit_column_group',{'claim':{'bad':CANARY}})['code']=='invalid_proposal_arguments'
    assert session.execute('declare_semantic_schema',{'entity_types':[
        {'name':'SaleType','description':'First business definition'}]})['status']=='ok'
    assert session.execute('declare_semantic_schema',{'entity_types':[
        {'name':'SaleType','description':'Replacement definition'}]})['code']=='semantic_definition_conflict'
    batch=session.batch()
    assert len(batch.proposals)==1
    assert batch.semantic_schema.entity_types[0].description=='First business definition'
    assert batch._runtime_invalid_drafts==2
    assert batch._runtime_pending_codes==['discovery_invalid_drafts']
    assert '_runtime_pending_codes' not in batch.model_dump_json()
    assert CANARY not in json.dumps(session.reject('invalid_proposal_arguments'))
    session.clear()
    assert session.drafts==[] and session.packet=={} and session.schema.is_empty()


def test_task_tools_and_reference_validation_do_not_grant_new_authority():
    for task,kinds in TASK_PROPOSAL_KINDS.items():
        specs=proposal_tool_specs(task)
        names={spec['function']['name'] for spec in specs}
        for kind in ('column_group','reference','wide_hierarchy','exclusion'):
            expected=kind in kinds and (kind!='wide_hierarchy' or task=='T5_revision')
            assert ('submit_'+kind in names)==expected
        assert ('submit_hierarchy_plan' in names)==('wide_hierarchy' in kinds)
        for spec in specs:
            Draft202012Validator.check_schema(spec['function']['parameters'])
    session=ProposalSession(input_packet())
    invalid=group();invalid['evidence_cited']=['unissued_probe']
    assert session.execute('submit_column_group',invalid)['code']=='dangling_evidence_reference'
    assert not session.drafts
    assert session.execute('submit_reference',{})['code']=='task_kind_forbidden'
    with pytest.raises(ValidationError):
        ProposalBatch(task='T1_table_discovery',_runtime_pending_codes=[])


@pytest.mark.parametrize('declared_in_session',[False,True])
def test_plan_reuses_existing_business_definition_without_replacing_it(declared_in_session):
    from lightgraph.tests.test_hierarchy_plan import fixture
    _,packet,plan=fixture()
    schema={'entity_types':[{'name':plan['entity_type'],
        'description':'A catalog business object independent of its position in a displayed path.'}]}
    if not declared_in_session:packet['semantic_schema']=schema
    session=ProposalSession(packet)
    if declared_in_session:
        assert session.execute('declare_semantic_schema',schema)['status']=='ok'
    result=session.execute('submit_hierarchy_plan',plan)
    assert result=={'status':'ok','draft_id':'draft_1','confirmed':False}
    batch=session.batch()
    assert batch.semantic_schema.model_dump()['entity_types']==schema['entity_types']
    assert len(batch.proposals)==1 and not batch._runtime_pending_codes
    assert session.invalid_count==0
    conflicting={'entity_types':[{'name':plan['entity_type'],
        'description':'A different interpretation that must not overwrite the declared meaning.'}]}
    assert session.execute('declare_semantic_schema',conflicting)['code']=='semantic_definition_conflict'
    assert session.batch().semantic_schema.model_dump()['entity_types']==schema['entity_types']


def test_hidden_legacy_wide_submission_remains_available_for_exact_internal_revision():
    from lightgraph.tests.test_hierarchy_plan import fixture
    from lightgraph.discovery.hierarchy_plan import compile_hierarchy_plan
    _,packet,plan=fixture()
    proposal=compile_hierarchy_plan(plan,packet).proposals[0]
    session=ProposalSession(packet)
    assert 'submit_wide_hierarchy' not in session.names
    result=session.execute('submit_wide_hierarchy',proposal.model_dump(mode='json',exclude={'kind'}))
    assert result['status']=='ok' and session.batch().proposals==[proposal]
    revision=ProposalSession({**packet,'task':'T5_revision'})
    assert 'submit_wide_hierarchy' in revision.names
    assert revision.execute('submit_wide_hierarchy',proposal.model_dump(mode='json',exclude={'kind'}))['status']=='ok'


def test_runtime_declares_business_type_then_submits_plan_in_one_turn(framework_spy):
    from lightgraph.tests.test_hierarchy_plan import fixture
    _,packet,plan=fixture()
    packet['probe_digest'][0]['operator']='profile_columns'
    schema={'entity_types':[{'name':plan['entity_type'],
        'description':'A defined business object, with identities independent of relative path slots.'}]}
    def handler(request):
        names={item['function']['name'] for item in json.loads(request.content)['tools']}
        assert 'submit_hierarchy_plan' in names and 'submit_wide_hierarchy' not in names
        return tools_response(('declare_semantic_schema',schema),
            ('submit_hierarchy_plan',plan),('finish_discovery',{}))
    client=model(handler,stream=False);tools=Toolbox()
    batch=client.explore(packet,tools,max_steps=1)
    assert len(batch.proposals)==1 and batch.proposals[0].kind=='wide_hierarchy'
    assert batch.semantic_schema.model_dump()['entity_types']==schema['entity_types']
    assert not batch._runtime_pending_codes and batch._runtime_invalid_drafts==0
    assert client.usage['calls']==1 and tools.executed==[]
    assert_cleared(framework_spy)


@pytest.mark.parametrize('finish_tool',[False,True])
def test_real_runtime_stages_then_finishes_without_full_batch(finish_tool,framework_spy):
    bodies=[]
    def handler(request):
        body=json.loads(request.content);bodies.append(body)
        assert 'response_format' not in body  # Tool turns have no competing final-output contract.
        if len(bodies)==1:
            return response(tool='submit_column_group',args=group())
        assert json.loads(body['messages'][-1]['content'])['draft_id']=='draft_1'
        if finish_tool:
            return response(tool='finish_discovery')
        return response({'action':'finish_discovery','pending':False})
    client=model(handler,stream=False);tools=Toolbox()
    batch=client.explore(input_packet(),tools,max_steps=4,max_tool_calls=3)
    assert len(batch.proposals)==1 and not batch._runtime_pending_codes
    assert client.usage['calls']==2 and tools.executed==[] and tools.closed
    assert_cleared(framework_spy)


def test_invalid_submission_recovers_next_tool_and_retains_valid_sibling(framework_spy,capsys,caplog):
    calls=[]
    def handler(request):
        body=json.loads(request.content);calls.append(body)
        if len(calls)==1:return response(tool='submit_column_group',args=group())
        if len(calls)==2:return response(tool='submit_entity_key',args={'claim':{'invalid':CANARY}})
        error=json.loads(body['messages'][-1]['content'])
        assert error['code']=='invalid_tool_arguments' and CANARY not in json.dumps(error)
        return response(tool='finish_discovery')
    client=model(handler,stream=False);tools=Toolbox()
    batch=client.explore(input_packet(),tools,max_steps=5)
    assert len(batch.proposals)==1 and batch._runtime_invalid_drafts==1
    assert batch._runtime_pending_codes==[]  # Explicit finish withdraws the rejected attempt.
    assert client.usage['calls']==3 and tools.executed==[]
    assert_cleared(framework_spy)
    assert CANARY not in capsys.readouterr().out+caplog.text


def test_reserved_submission_window_allows_field_repair_before_final_turn(framework_spy):
    controls=[]
    def handler(request):
        body=json.loads(request.content)
        control=json.loads(body['messages'][1]['content'])['runtime_control']
        controls.append(control)
        if len(controls)<=2:
            return response(tool='get_metadata' if len(controls)==1 else 'profile_columns')
        names={item['function']['name'] for item in body['tools']}
        assert names and not (names & {'get_metadata','profile_columns','execute_readonly_sql'})
        if len(controls)==3:
            assert control['submission_reason']=='submission_window'
            return response(tool='submit_column_group',args={'claim':{'invalid':CANARY}})
        assert control['submission_reason']=='last_turn'
        feedback=json.loads(body['messages'][-1]['content'])
        assert feedback['code']=='invalid_tool_arguments' and feedback['diagnostics']
        return tools_response(('submit_column_group',group()),('finish_discovery',{}))
    client=model(handler,stream=False);tools=Toolbox()
    batch=client.explore(input_packet(),tools,max_steps=4,max_tool_calls=16)
    assert len(batch.proposals)==1 and not batch._runtime_pending_codes
    assert batch._runtime_invalid_drafts==1
    assert client.usage['calls']==4 and tools.executed==['get_metadata','profile_columns']
    assert_cleared(framework_spy)


@pytest.mark.parametrize('failure',['malformed','transport','ignored_none','truncated','duplicate',
    'model_stream_event_too_large','model_stream_transport_too_large'])
def test_failed_final_preserves_draft_but_marks_pending_without_extra_turn(failure,framework_spy):
    calls=[]
    def handler(request):
        body=json.loads(request.content);calls.append(body)
        if len(calls)==1:return response(tool='submit_column_group',args=group())
        assert body['tool_choice']=='auto'
        assert all(item['function']['name'].startswith(('submit_', 'declare_', 'finish_')) for item in body['tools'])
        if failure=='transport':raise httpx.ReadError(CANARY)
        if failure.startswith('model_stream_'):
            from lightgraph.errors import LightGraphError
            raise LightGraphError(failure)
        if failure=='ignored_none':return response(tool='execute_readonly_sql')
        if failure=='truncated':
            return httpx.Response(200,json={'choices':[{'message':{'content':'{}'},'finish_reason':'length'}]})
        if failure=='duplicate':
            return httpx.Response(200,json={'choices':[{'message':{
                'content':'{"action":"finish_discovery","action":"abstain"}'},'finish_reason':'stop'}]})
        return httpx.Response(200,json={'choices':[{'message':{'content':'{unfinished'},'finish_reason':'stop'}]})
    client=model(handler,stream=False,retries=0);tools=Toolbox()
    batch=client.explore(input_packet(),tools,max_steps=2,max_tool_calls=1)
    assert len(batch.proposals)==1
    assert batch._runtime_pending_codes==['discovery_submission_incomplete']
    assert client.usage['calls']==2 and tools.executed==[] and tools.closed
    assert_cleared(framework_spy)


def test_staged_exclusion_cannot_be_overwritten_by_legacy_final(framework_spy):
    calls=[]
    def handler(request):
        calls.append(1)
        if len(calls)==1:
            return response(tool='submit_exclusion',args={'claim':{'exclusions':[
                {'column_id':'c_a','reason':'sensitive','affects':['identity','projection']}]},
                'evidence_cited':['c_a']})
        return response(final())
    client=model(handler,stream=False);tools=Toolbox()
    batch=client.explore(input_packet(),tools)
    assert [item.kind for item in batch.proposals]==['exclusion','column_group']
    assert batch.proposals[0].claim.exclusions[0].affects==['identity','projection']
    assert_cleared(framework_spy)


@pytest.mark.parametrize('legacy',[False,True])
def test_explicit_global_abstention_withdraws_all_staged_proposals(legacy,framework_spy):
    calls=[]
    def handler(request):
        calls.append(1)
        if len(calls)==1:return response(tool='submit_column_group',args=group())
        return response({'task':'T1_table_discovery','abstain':True} if legacy
                        else {'action':'abstain'})
    client=model(handler,stream=False);tools=Toolbox()
    batch=client.explore(input_packet(),tools)
    assert batch.abstain and batch.proposals==[]
    assert batch._runtime_pending_codes==[] and batch._runtime_invalid_drafts==0
    assert client.usage['calls']==2 and tools.executed==[]
    assert_cleared(framework_spy)


def test_pending_finish_preserves_drafts_without_treating_it_as_abstention(framework_spy):
    calls=[]
    def handler(request):
        calls.append(1)
        if len(calls)==1:return response(tool='submit_column_group',args=group())
        return response({'action':'finish_discovery','pending':True})
    client=model(handler,stream=False);tools=Toolbox()
    batch=client.explore(input_packet(),tools)
    assert not batch.abstain and len(batch.proposals)==1
    assert batch._runtime_pending_codes==['discovery_submission_pending']
    assert client.usage['calls']==2 and tools.executed==[]
    assert_cleared(framework_spy)


def test_submission_cannot_bypass_metadata_only_source_policy(framework_spy):
    calls=[]
    def handler(request):
        body=json.loads(request.content);calls.append(body)
        if len(calls)==1:return response(tool='execute_readonly_sql')
        if len(calls)==2:
            assert json.loads(body['messages'][-1]['content'])['code']=='source_samples_disabled'
            return response(tool='submit_column_group',args=group())
        return response({'action':'finish_discovery'})
    client=model(handler,stream=False,allow_source_samples=False);tools=Toolbox()
    assert client.explore(input_packet(),tools).proposals
    assert tools.executed==[] and client.usage['calls']==3
    assert_cleared(framework_spy)


@pytest.mark.parametrize('finish',[False,True])
@pytest.mark.parametrize('source_first',[False,True])
def test_final_turn_still_accepts_hierarchy_plan_after_source_budget_exhaustion(
        finish,source_first,framework_spy):
    from lightgraph.tests.test_hierarchy_plan import fixture
    _,packet,plan=fixture()
    packet['probe_digest'][0]['operator']='profile_columns'
    requests=[]
    def handler(request):
        body=json.loads(request.content);requests.append(body)
        if source_first and len(requests)==1:
            return response(tool='get_metadata')
        names={spec['function']['name'] for spec in body['tools']}
        assert 'submit_hierarchy_plan' in names and 'finish_discovery' in names
        assert 'get_metadata' not in names and 'execute_readonly_sql' not in names
        operations=[('submit_hierarchy_plan',plan)]
        if finish:operations.append(('finish_discovery',{}))
        return tools_response(*operations)
    client=model(handler,stream=False);tools=Toolbox()
    steps=2 if source_first else 1
    batch=client.explore(packet,tools,max_steps=steps,max_tool_calls=1)
    assert len(batch.proposals)==1 and batch.proposals[0].kind=='wide_hierarchy'
    assert batch.semantic_schema.entity_types[0].name=='CatalogEntry'
    assert batch._runtime_pending_codes==([] if finish else ['discovery_submission_incomplete'])
    assert client.usage['calls']==steps
    assert tools.executed==(['get_metadata'] if source_first else [])
    agent=assert_cleared(framework_spy)
    assert agent.model.source_calls==int(source_first)
    assert agent.model.submission_calls==1+int(finish)


def test_submissions_have_separate_finite_cap_and_do_not_expand_query_budget(framework_spy):
    from lightgraph.discovery.proposal_session import MAX_SUBMISSION_CALLS
    requests=[]
    def handler(request):
        requests.append(1)
        if len(requests)==1:
            return response(tool='submit_column_group',args=group())
        return tools_response(*[('declare_semantic_schema',{})]*MAX_SUBMISSION_CALLS)
    client=model(handler,stream=False);tools=Toolbox()
    batch=client.explore(input_packet(),tools,max_steps=4,max_tool_calls=1)
    assert len(batch.proposals)==1 and batch._runtime_pending_codes==['discovery_submission_incomplete']
    assert client.usage['calls']==2 and tools.executed==[]
    agent=assert_cleared(framework_spy)
    assert agent.model.source_calls==0 and agent.model.submission_calls==1
