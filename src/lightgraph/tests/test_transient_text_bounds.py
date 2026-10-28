"""Reasoning length is bounded without truncating semantics or retaining prose."""
import json

import pytest
from pydantic import ValidationError

from lightgraph.discovery.models import ProposalBase, ProposalBatch, TaskPacket
from lightgraph.discovery.semantics import (ConditionReview, EvidenceInterpretation,
    SemanticReview, review_semantics, rule_evidence)
from lightgraph.errors import LightGraphError
from lightgraph.store import GraphStore
from lightgraph.tests.test_discovery_agent import client, input_packet, response, Toolbox
from lightgraph.tests.test_relative_hierarchy import execute, path, rule


CANARY='EPHEMERAL_LONG_REASON_8172'


def prose(length):
    text=CANARY+'。保留原始限制，不得删除否定条件；证据不足时保持未决。'
    return (text*(length//len(text)+1))[:length]


def hierarchy_batch(length):
    return {'task':'T1_table_discovery','notes':prose(length),
        'proposals':[{'kind':'wide_hierarchy','rationale':prose(length),
            'claim':{'group_id':'tree','levels':[{'position':1,'key':'c_a'},
                                               {'position':2,'key':'c_b'}]},
            'evidence_cited':['c_a','c_b']}]}


@pytest.mark.parametrize('length',[900,1500,8192])
def test_long_proposal_text_is_verbatim_without_schema_retry(monkeypatch,length,capsys,caplog):
    original=hierarchy_batch(length);calls=[]
    def handler(request):
        calls.append(1)
        return response(original)
    model=client(monkeypatch,handler);tools=Toolbox()
    checked=model.explore(input_packet(),tools)
    assert checked.notes==original['notes'] and len(checked.notes)==length
    assert checked.proposals[0].rationale==original['proposals'][0]['rationale']
    assert calls==[1] and model.usage['calls']==1
    assert model.usage.get('schema_repair_attempts',0)==0
    assert model.config.max_prompt_bytes==65536 and model.config.max_response_bytes==262144
    assert tools.closed and not tools.executed
    assert CANARY not in json.dumps(model.usage)+capsys.readouterr().out+caplog.text


@pytest.mark.parametrize('contract,field,required',[
    (ProposalBase,'rationale',{'evidence_cited':['c_a']}),
    (ProposalBatch,'notes',{'task':'T1_table_discovery'}),
    (EvidenceInterpretation,'explanation',{'reference':'probe_1','stance':'supports'}),
    (ConditionReview,'explanation',{'state':'satisfied','evidence_refs':['probe_1']}),
])
def test_all_four_transient_fields_keep_exact_limit_and_reject_overflow(contract,field,required):
    text=prose(8192)
    checked=contract.model_validate({**required,field:text})
    assert getattr(checked,field)==text
    with pytest.raises(ValidationError) as caught:
        contract.model_validate({**required,field:prose(8193)})
    assert [(item['loc'],item['type']) for item in caught.value.errors(include_input=False)]==[
        ((field,),'string_too_long')]


def test_packet_advertises_same_rationale_limit_as_response_schema():
    from lightgraph.contracts import ExtractionGoal
    from lightgraph.discovery.engine import _packet, _understanding
    from lightgraph.tests.test_agent_pipeline import table
    packet=TaskPacket(task='T1_table_discovery')
    assert packet.budget.max_rationale_chars==8192
    assert ProposalBatch.model_json_schema()['$defs']['HierarchyProposal']['properties']['rationale']['maxLength']==8192
    assert TaskPacket.model_json_schema()['$defs']['PacketBudget']['properties']['max_rationale_chars']['maximum']==8192
    source=table('objects',['code'])
    generated=_packet(source,ExtractionGoal(),[],[],'T1_table_discovery',_understanding(source,[],{},[]))
    assert generated['budget']['max_rationale_chars']==8192


def review_reply(packet,length,*,decision='supported',stance='supports',state='satisfied'):
    return {'proposition_ref':packet['semantic_proposition_ref'],'decision':decision,
        'basis':'data_inference','evidence':[{'reference':'probe_1','stance':stance,
            'explanation':prose(length)}],
        'conditions':[{'state':state,'evidence_refs':['probe_1'],'explanation':prose(length)}]}


@pytest.mark.parametrize('length',[900,1500])
def test_long_review_explanations_survive_parsing_without_schema_retry(monkeypatch,length):
    packet={'semantic_proposition_ref':'issued_proposition'};calls=[]
    def handler(request):
        calls.append(1)
        return response(review_reply(packet,length))
    model=client(monkeypatch,handler)
    checked=model.complete('semantic_review',packet,SemanticReview)
    assert checked.evidence[0].explanation==prose(length)
    assert checked.conditions[0].explanation==prose(length)
    assert calls==[1] and model.usage.get('schema_repair_attempts',0)==0


@pytest.mark.parametrize('decision,stance,state,code',[
    ('refuted','refutes','satisfied','semantic_claim_refuted'),
    ('supported','refutes','satisfied','semantic_claim_refuted'),
    ('unresolved','context','satisfied','semantic_claim_unresolved'),
    ('supported','supports','unknown','semantic_condition_unresolved'),
    ('supported','supports','unsatisfied','semantic_condition_unresolved'),
])
def test_long_negative_or_unknown_review_still_stops_without_retry(monkeypatch,decision,stance,state,code):
    calls=[]
    def handler(request):
        calls.append(1)
        packet=json.loads(request.content)['messages'][1]['content']
        packet=json.loads(packet)['input']
        return response(review_reply(packet,1500,decision=decision,stance=stance,state=state))
    model=client(monkeypatch,handler)
    with pytest.raises(LightGraphError,match=code):
        review_semantics(model,{'probe_digest':[{'probe_id':'probe_1'}]},
            {'kind':'wide_hierarchy','claim':{}},[])
    assert calls==[1] and model.usage.get('schema_repair_attempts',0)==0


def test_long_explanations_never_enter_rule_evidence_or_result_store(monkeypatch,tmp_path,capsys,caplog):
    def handler(request):
        packet=json.loads(json.loads(request.content)['messages'][1]['content'])['input']
        return response(review_reply(packet,1500))
    model=client(monkeypatch,handler)
    refs=review_semantics(model,{'probe_digest':[{'probe_id':'probe_1'}]},
        {'kind':'wide_hierarchy','claim':{}},[])
    evidence=rule_evidence(refs,reviewed=True)
    assert CANARY not in evidence.model_dump_json()+str(refs)
    spec=rule();spec.evidence=evidence
    database=tmp_path/'graph.sqlite'
    with GraphStore(database) as store:
        assert execute(store,spec,[path('A','B')]).published
        assert CANARY not in store.get_rules()[0].model_dump_json()
    assert CANARY.encode() not in database.read_bytes()
    assert CANARY not in json.dumps(model.usage)+capsys.readouterr().out+caplog.text


def test_larger_field_limit_does_not_override_whole_response_byte_limit(monkeypatch):
    calls=[]
    def handler(request):
        calls.append(1)
        return response(hierarchy_batch(1500))
    model=client(monkeypatch,handler);model.config.max_response_bytes=4096
    with pytest.raises(LightGraphError,match='model_response_too_large'):
        model.explore(input_packet(),Toolbox())
    assert calls==[1] and model.config.max_response_bytes==4096
    assert model.usage.get('schema_repair_attempts',0)==0
