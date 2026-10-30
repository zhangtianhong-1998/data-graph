"""Provider constraints prevent malformed output; local trust gates remain final."""
import json
from copy import deepcopy

import httpx
import pytest
from pydantic import ValidationError

from lightgraph.contracts import canonical
from lightgraph.discovery.llm import ModelConfig, response_format_for, request_envelope_for
from lightgraph.discovery.models import ProposalBatch
from lightgraph.discovery.proposal_session import DiscoveryFinish
from lightgraph.discovery.semantics import review_semantics
from lightgraph.errors import LightGraphError
from lightgraph.pipeline import make_model
from lightgraph.runtime.config import AgentConfig, ModelConfig as RuntimeModelConfig, RunConfig
from lightgraph.tests.test_discovery_agent import client, input_packet, final, response, Toolbox
from lightgraph.tests.test_schema_protocol_correction import malformed_hierarchy, corrected_hierarchy


def assert_strict(body,schema):
    assert body['response_format']=={'type':'json_schema','json_schema':{
        'name':schema['title'],'strict':True,'schema':schema}}


@pytest.mark.parametrize('mode',['json_schema','json_object'])
def test_mode_defaults_and_runtime_configuration_pass_through_without_secret_dump(mode,monkeypatch):
    settings=RuntimeModelConfig(mode='metadata_only',base_url='https://model.invalid/v1',model='offline',
        response_format=mode,api_key='UNRETAINED_CONFIG_KEY')
    assert AgentConfig(llm=settings).llm.response_format==mode
    model=make_model(RunConfig(sources=[],model=settings))
    assert model.config.response_format==mode
    assert 'UNRETAINED_CONFIG_KEY' not in settings.model_dump_json()+repr(settings)
    monkeypatch.setenv('LIGHTGRAPH_RESPONSE_FORMAT',mode)
    assert ModelConfig.from_env().response_format==mode
    assert RuntimeModelConfig().response_format=='json_schema'
    assert ModelConfig(base_url='https://model.invalid/v1',model='offline').response_format=='json_schema'
    with pytest.raises(ValidationError):RuntimeModelConfig(response_format='auto')


def test_small_finish_schema_is_sent_while_submissions_remain_typed_tools(monkeypatch):
    calls=[];schema=DiscoveryFinish.model_json_schema()
    def handler(request):
        body=json.loads(request.content);calls.append(body)
        assert 'response_format' not in body
        envelope=json.loads(body['messages'][1]['content'])
        assert envelope['output_schema']==schema
        if len(calls)==1:
            assert body.get('tools')
            return response(tool='get_metadata')
        assert body.get('tools') and all(item['function']['name'].startswith(
            ('submit_','declare_','finish_')) for item in body['tools'])
        return response(final())
    model=client(monkeypatch,handler);tools=Toolbox()
    assert model.explore(input_packet(),tools,max_steps=2).proposals
    assert len(calls)==2 and tools.executed==['get_metadata']
    assert set(schema['properties'])=={'action','pending'}
    assert calls[0]['tools'][0]['function']['name']=='get_metadata'


def test_correction_uses_same_strict_schema_without_tool_definitions(monkeypatch):
    original=malformed_hierarchy();calls=[]
    def handler(request):
        body=json.loads(request.content);calls.append(body)
        assert_strict(body,ProposalBatch.model_json_schema())
        if len(calls)==1:return response(original)
        assert not body.get('tools')
        return response(corrected_hierarchy(original))
    model=client(monkeypatch,handler)
    assert model.complete('T1_table_discovery',input_packet(),ProposalBatch).proposals
    assert len(calls)==2 and model.usage['schema_repairs_succeeded']==1


def test_provider_strict_mode_does_not_override_local_extra_field_preservation(monkeypatch):
    original=final();original['unknown_constraint']='Do not publish this interpretation.';calls=[]
    def handler(request):
        calls.append(1)
        return response(original if len(calls)==1 else final())
    with pytest.raises(LightGraphError,match='llm_schema_invalid'):
        client(monkeypatch,handler).explore(input_packet(),Toolbox())
    assert calls==[1,1]


def test_strict_semantic_response_still_rejects_negative_judgment(monkeypatch):
    calls=[]
    def handler(request):
        body=json.loads(request.content);calls.append(body)
        assert body['response_format']['json_schema']['name']=='BoundSemanticVerdict'
        packet=json.loads(body['messages'][1]['content'])['input']
        return response({'proposition_ref':packet['semantic_proposition_ref'],'decision':'refuted',
            'basis':'data_inference','evidence':[{'reference':'probe_1','stance':'refutes'}]})
    with pytest.raises(LightGraphError,match='semantic_claim_refuted'):
        review_semantics(client(monkeypatch,handler),{'probe_digest':[{'probe_id':'probe_1'}]},
            {'kind':'wide_hierarchy'},[])
    assert len(calls)==1


@pytest.mark.parametrize('status',[400,422])
def test_strict_provider_rejection_is_fixed_and_never_silently_downgrades(monkeypatch,status,capsys,caplog):
    calls=[]
    def handler(request):
        calls.append(json.loads(request.content)['response_format']['type'])
        return httpx.Response(status,text='UNRETAINED_PROVIDER_ERROR_BODY')
    with pytest.raises(LightGraphError,match='model_structured_output_rejected') as caught:
        client(monkeypatch,handler).complete('T1_table_discovery',input_packet(),ProposalBatch)
    assert calls==['json_schema']
    assert 'UNRETAINED_PROVIDER_ERROR_BODY' not in str(caught.value)+capsys.readouterr().out+caplog.text


def test_explicit_json_object_preserves_compatibility_transport(monkeypatch):
    calls=[]
    def handler(request):
        body=json.loads(request.content);calls.append(body)
        assert body['response_format']=={'type':'json_object'}
        return response(final())
    model=client(monkeypatch,handler);model.config.response_format='json_object'
    assert model.complete('T1_table_discovery',input_packet(),ProposalBatch).proposals and len(calls)==1


def test_exact_prompt_threshold_includes_entire_response_format(monkeypatch):
    schema=ProposalBatch.model_json_schema()
    messages=[{'role':'user','content':canonical({'task':'T1_table_discovery','output_schema':schema})}]
    envelope=request_envelope_for(messages,[],response_format_for(schema,'json_schema'))
    threshold=len(canonical(envelope).encode());calls=[]
    def handler(request):calls.append(1);return response(final())
    model=client(monkeypatch,handler);model.config.max_prompt_bytes=threshold
    assert model._request(messages,lambda message:ProposalBatch.model_validate_json(message['content'])).proposals
    blocked=client(monkeypatch,handler);blocked.config.max_prompt_bytes=threshold-1
    with pytest.raises(LightGraphError,match='model_packet_too_large'):
        blocked._request(messages,lambda message:ProposalBatch.model_validate_json(message['content']))
    assert calls==[1] and blocked.usage['calls']==0 and blocked.budget.snapshot()['reserved']=={}


def test_strict_mode_requires_an_output_schema_and_uses_only_safe_provider_names(monkeypatch):
    model=client(monkeypatch,lambda _:pytest.fail('schema-less strict requests must not reach provider'))
    with pytest.raises(LightGraphError,match='model_output_schema_missing'):
        model._request([],lambda value:value)
    schema={'type':'object','title':'unsafe title / arbitrary','additionalProperties':False,'properties':{}}
    shape=response_format_for(schema,'json_schema')
    assert shape['json_schema']['name']=='lightgraph_response'
    assert shape['json_schema']['schema']==schema


@pytest.mark.parametrize('mode',['json_schema','json_object'])
def test_schema_dedup_only_changes_matching_task_envelope_and_never_input_or_original_messages(mode):
    schema=ProposalBatch.model_json_schema()
    same={'task':'T1_table_discovery','output_schema':schema}
    packet={**same,'input':{'untrusted_document':same}}
    messages=[{'role':'user','content':canonical(packet)},
        {'role':'tool','content':canonical(same)},
        {'role':'assistant','content':canonical(same)},
        {'role':'user','content':canonical({'output_schema':schema})},
        {'role':'user','content':canonical({**same,'output_schema':{'different':'contract'}})}]
    original=deepcopy(messages)
    envelope=request_envelope_for(messages,[],response_format_for(schema,mode))
    assert messages==original and envelope['messages'][1:]==messages[1:]
    sent=json.loads(envelope['messages'][0]['content'])
    assert sent['input']==packet['input']
    assert sent['output_schema']==({'schema_ref':'response_format.json_schema.schema'} if mode=='json_schema' else schema)
