"""Cosmetic JSON recovery keeps the entire candidate and every negative guard."""
from copy import deepcopy
import json

import httpx
import pytest

from lightgraph.discovery.llm import _schema_correction_anchor, _invalid_review_terminal
from lightgraph.discovery.models import ProposalBatch
from lightgraph.discovery.semantics import review_semantics
from lightgraph.errors import BudgetExceeded, LightGraphError
from lightgraph.tests.test_discovery_agent import client, input_packet, Toolbox, response
from lightgraph.tests.test_schema_protocol_correction import (malformed_hierarchy,
    corrected_hierarchy, invalid_slot_batch, legacy_batch)


CANARY='UNRETAINED_JSON_TEXT_3629'


def wire(content,finish='stop'):
    return httpx.Response(200,json={'choices':[{'message':{'content':content},'finish_reason':finish}],
        'usage':{'prompt_tokens':30,'completion_tokens':20}})


def decorate(value,kind):
    text=json.dumps(value,ensure_ascii=False)
    if kind in {'comma','combined'}:text=text[:-1]+',}'
    if kind in {'control','combined'}:text=text.replace('\\n','\n')
    if kind in {'fence','combined'}:text='```json\n'+text+'\n```'
    if kind in {'bom','combined'}:text='\ufeff'+text
    return text


def candidate():
    value=corrected_hierarchy(malformed_hierarchy())
    value['notes']=CANARY+'\nDo not remove the exclusion; literal ,} and ,] are prose.'
    value['proposals'][0]['rationale']='Only this namespace applies; do not merge across sources.'
    return value


@pytest.mark.parametrize('kind',['fence','bom','comma','control','combined'])
def test_cosmetic_legacy_final_recovers_on_last_step_without_source_tools_or_extra_call(monkeypatch,kind,capsys,caplog):
    value=candidate();calls=[]
    def handler(request):
        calls.append(1)
        body=json.loads(request.content)
        assert all(item['function']['name'].startswith(('submit_','declare_','finish_'))
                   for item in body.get('tools',[]))
        return wire(decorate(value,kind))
    model=client(monkeypatch,handler);tools=Toolbox()
    checked=model.explore(input_packet(),tools,max_steps=1)
    assert checked==ProposalBatch.model_validate(value)
    assert calls==[1] and model.usage['calls']==1 and model.usage['failures']==0
    assert model.usage['json_syntax_recoveries']==1
    assert model.usage.get('schema_repair_attempts',0)==0
    assert not tools.executed and tools.closed and model.budget.snapshot()['reserved']=={}
    assert CANARY not in capsys.readouterr().out+caplog.text+json.dumps(model.usage)


@pytest.mark.parametrize('drop_exclusion',[False,True])
def test_cosmetic_recovery_then_schema_repair_locks_valid_siblings_and_exclusions(monkeypatch,drop_exclusion):
    original,fixed,_=invalid_slot_batch()
    original['notes']=fixed['notes']='Do not merge tenant identities.'
    if drop_exclusion:fixed['proposals']=[p for p in fixed['proposals'] if p['kind']!='exclusion']
    calls=[]
    def handler(request):
        calls.append(1)
        body=json.loads(request.content)
        if len(calls)==1:return wire(decorate(original,'combined'))
        assert not body.get('tools')
        feedback=json.loads(body['messages'][-1]['content'])['schema_protocol_correction']
        assert json.loads(feedback['previous_candidate'])==original
        return response(fixed)
    model=client(monkeypatch,handler)
    if drop_exclusion:
        with pytest.raises(LightGraphError,match='llm_schema_invalid'):
            legacy_batch(model,input_packet(),Toolbox())
    else:
        assert legacy_batch(model,input_packet(),Toolbox())==ProposalBatch.model_validate(fixed)
    assert calls==[1,1] and model.usage['json_syntax_recoveries']==1


@pytest.mark.parametrize('decision,state,code',[
    ('refuted','satisfied','semantic_claim_refuted'),
    ('supported','unknown','semantic_condition_unresolved')])
def test_cosmetic_negative_review_cannot_trigger_a_positive_retry(monkeypatch,decision,state,code):
    calls=[]
    def handler(request):
        calls.append(1)
        packet=json.loads(json.loads(request.content)['messages'][1]['content'])['input']
        return wire(decorate({'proposition_ref':packet['semantic_proposition_ref'],
            'decision':decision,'basis':'data_inference','evidence':[{'reference':'probe_1',
                'stance':'refutes' if decision=='refuted' else 'supports','explanation':CANARY+'\nnegative'}],
            'conditions':[{'state':state,'evidence_refs':['probe_1'],'explanation':'Keep this restriction.'}]},'combined'))
    model=client(monkeypatch,handler)
    with pytest.raises(LightGraphError,match=code):
        review_semantics(model,{'probe_digest':[{'probe_id':'probe_1'}]}, {'kind':'wide_hierarchy'},[])
    assert calls==[1] and model.usage.get('schema_repair_attempts',0)==0


@pytest.mark.parametrize('bad_kind',['truncated','unclosed_string','outside_prose','two_objects','empty_item'])
def test_unrecoverable_suffix_or_extra_content_cannot_be_replaced_by_an_unbound_batch(monkeypatch,bad_kind):
    value=candidate();raw=json.dumps(value)
    malformed={'truncated':raw[:-1],'unclosed_string':'{"notes":"'+CANARY,
        'outside_prose':'Do not publish this interpretation.\n'+raw,
        'two_objects':raw+'{"exclude":"'+CANARY+'"}',
        'empty_item':raw.replace('"c_a", "c_b"','"c_a",, "c_b"')}[bad_kind]
    calls=[]
    def handler(request):
        calls.append(1)
        if len(calls)==1:return wire(malformed)
        feedback=json.loads(json.loads(request.content)['messages'][-1]['content'])['schema_protocol_correction']
        diagnostic=feedback['syntax_diagnostic']
        assert CANARY not in json.dumps(diagnostic)
        assert diagnostic['content_chars']==len(malformed)
        return response(value)
    model=client(monkeypatch,handler)
    with pytest.raises(LightGraphError,match='llm_schema_invalid'):
        model.explore(input_packet(),Toolbox())
    assert calls==[1,1] and model.usage.get('json_syntax_recoveries',0)==0


@pytest.mark.parametrize('raw,code',[
    ('{"task":"T1_table_discovery","notes":"deny","notes":"","proposals":[]}', 'model_json_duplicate_key'),
    ('{"decision":"refuted","decision":"supported"}', 'model_json_duplicate_key'),
    ('{"notes":NaN}', 'model_json_nonfinite'),
    ('{"notes":Infinity}', 'model_json_nonfinite'),
    ('{"notes":1e999}', 'model_json_nonfinite'),
])
def test_ambiguous_json_is_terminal_and_cannot_build_a_last_wins_anchor(monkeypatch,raw,code):
    calls=[]
    def handler(request):calls.append(1);return wire(raw)
    model=client(monkeypatch,handler)
    with pytest.raises(LightGraphError,match=code):model.explore(input_packet(),Toolbox())
    assert calls==[1]
    assert not _schema_correction_anchor({'content':raw},ProposalBatch)['verifiable']
    assert _invalid_review_terminal({'content':raw})=='semantic_review_protocol_invalid'


def test_provider_truncation_remains_terminal_even_when_text_could_parse(monkeypatch):
    calls=[]
    def handler(request):calls.append(1);return wire(decorate(candidate(),'fence'),'length')
    model=client(monkeypatch,handler)
    with pytest.raises(LightGraphError,match='model_response_truncated'):
        model.explore(input_packet(),Toolbox())
    assert calls==[1]


def test_recovery_does_not_create_budget_for_a_required_schema_repair(monkeypatch):
    from dataclasses import replace
    original,_,_=invalid_slot_batch();calls=[]
    def handler(request):calls.append(1);return wire(decorate(original,'fence'))
    model=client(monkeypatch,handler)
    model.budget.limits=replace(model.budget.limits,llm_calls=1)
    with pytest.raises(BudgetExceeded):model.explore(input_packet(),Toolbox(),max_steps=1)
    assert calls==[1] and model.budget.snapshot()['reserved']=={}


def test_tool_arguments_are_not_repaired_as_final_json(monkeypatch):
    calls=[];tools=Toolbox()
    def handler(request):
        calls.append(1)
        if len(calls)==1:return response(tool='get_metadata',raw_args='{"x":1,}')
        return wire(decorate(candidate(),'fence'))
    model=client(monkeypatch,handler)
    assert model.explore(input_packet(),tools).proposals
    assert not tools.executed and calls==[1,1]


@pytest.mark.parametrize('duplicate',[False,True])
def test_final_only_review_refuses_tool_calls_even_with_parseable_positive_content(monkeypatch,duplicate):
    calls=[]
    def handler(request):
        calls.append(1)
        packet=json.loads(json.loads(request.content)['messages'][1]['content'])['input']
        raw='{"proposition_ref":'+json.dumps(packet['semantic_proposition_ref'])+','
        if duplicate:raw+='"decision":"refuted",'
        raw+='"decision":"supported","basis":"data_inference","evidence":[{"reference":"probe_1","stance":"supports"}]}'
        return httpx.Response(200,json={'choices':[{'message':{'content':raw,
            'tool_calls':[{'id':'unexpected-call','type':'function','function':{'name':'get_metadata','arguments':'{}'}}]},
            'finish_reason':'tool_calls'}]})
    model=client(monkeypatch,handler)
    with pytest.raises(LightGraphError,match='semantic_review_protocol_invalid'):
        review_semantics(model,{'probe_digest':[{'probe_id':'probe_1'}]}, {'kind':'wide_hierarchy'},[])
    assert calls==[1] and model.usage.get('schema_repair_attempts',0)==0


def test_final_only_proposal_call_does_not_ignore_unrequested_tool_calls(monkeypatch):
    calls=[]
    def handler(request):
        calls.append(1)
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(candidate()),
            'tool_calls':[{'id':'unexpected-call','type':'function','function':{'name':'get_metadata','arguments':'{}'}}]},
            'finish_reason':'tool_calls'}]})
    model=client(monkeypatch,handler)
    with pytest.raises(LightGraphError,match='llm_schema_invalid'):
        model.complete('T1_table_discovery',input_packet(),ProposalBatch)
    assert calls==[1]
