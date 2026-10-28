"""Compact model verdicts stay bound to one compiler-owned proposition."""
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
import httpx
from pydantic import ValidationError

from lightgraph.contracts import canonical
from lightgraph.discovery.semantics import (
    BoundSemanticVerdict, SemanticReview, review_semantics, rule_evidence,
)
from lightgraph.errors import LightGraphError
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import client, input_packet, response
from lightgraph.tests.test_relative_hierarchy import execute, path, rule


PACKET={'probe_digest':[{'probe_id':'probe_1'}]}
PROPOSITION={'kind':'wide_hierarchy','claim':{'path_semantics':'relative_path',
    'namespace_columns':['c_namespace'],'condition':'Do not merge across namespaces.'}}
CANARY='EPHEMERAL_BOUND_CONDITION_73264'


def verdict(**changes):
    return {'decision':'supported','evidence':[{'reference':'probe_1','stance':'supports'}],
        'conditions':[],**changes}


def test_compact_output_binds_original_proposition_and_keeps_conditions_ephemeral(monkeypatch,tmp_path,capsys,caplog):
    seen=[];original=deepcopy(PROPOSITION)
    def handler(request):
        body,packet=input_packet(request);seen.append(body)
        schema=body['response_format']['json_schema']['schema']
        assert schema==BoundSemanticVerdict.model_json_schema()
        assert set(schema['properties'])=={'decision','decision_scope','evidence','conditions'}
        assert set(schema['$defs']['BoundEvidence']['properties'])=={'reference','stance'}
        assert 'explanation' not in canonical(schema)
        assert packet['semantic_proposition']==original
        assert 'tools' not in body
        return response(verdict(conditions=[{'state':'satisfied','evidence_refs':['probe_1'],
            'condition':CANARY}]))
    model=client(monkeypatch,handler)
    refs=review_semantics(model,PACKET,PROPOSITION,[])
    assert PROPOSITION==original and len(seen)==1
    assert refs==['semantic_review:bound_model_review','probe_1']
    evidence=rule_evidence(refs,reviewed=True)
    assert evidence.semantic_decision=='model_review' and evidence.conditions_satisfied
    spec=rule();spec.evidence=evidence
    with GraphStore(tmp_path/'graph.sqlite') as store:
        assert execute(store,spec,[path('A','B')]).published
        assert CANARY not in store.get_rules()[0].model_dump_json()
    assert CANARY.encode() not in (tmp_path/'graph.sqlite').read_bytes()
    assert CANARY not in str(refs)+str(model.usage)+caplog.text+capsys.readouterr().out


@pytest.mark.parametrize('legacy_form',['ref','echo','both'])
def test_valid_legacy_verdict_still_uses_its_original_binding(monkeypatch,legacy_form):
    def handler(request):
        _,packet=input_packet(request)
        value=verdict(basis='data_inference')
        if legacy_form!='echo':value['proposition_ref']=packet['semantic_proposition_ref']
        if legacy_form!='ref':value['proposition']=packet['semantic_proposition']
        return response(value)
    assert review_semantics(client(monkeypatch,handler),PACKET,PROPOSITION,[])==[
        'semantic_review:data_inference','probe_1']


@pytest.mark.parametrize('change,code',[
    ('wrong_ref','semantic_review_reference_mismatch'),
    ('changed_echo','semantic_review_proposition_mismatch'),
    ('compact_with_replacement','llm_schema_invalid'),
])
def test_response_cannot_replace_caller_proposition_or_hide_legacy_mismatch(monkeypatch,change,code):
    calls=[]
    def handler(request):
        _,packet=input_packet(request);calls.append(packet)
        value=verdict()
        if change=='compact_with_replacement':value['proposition']={'kind':'different'}
        else:
            value.update(basis='data_inference',proposition_ref=packet['semantic_proposition_ref'])
            if change=='wrong_ref':value['proposition_ref']='not_the_current_proposition'
            else:value['proposition']={'kind':'different'}
        return response(value)
    with pytest.raises(LightGraphError,match=code):
        review_semantics(client(monkeypatch,handler),PACKET,PROPOSITION,[])
    assert len(calls)==(2 if change=='compact_with_replacement' else 1)
    assert all(p['semantic_proposition']==PROPOSITION for p in calls)


@pytest.mark.parametrize('changes,code',[
    ({'decision':'refuted'},'semantic_claim_refuted'),
    ({'decision':'unresolved'},'semantic_claim_unresolved'),
    ({'evidence':[{'reference':'probe_1','stance':'refutes'}]},'semantic_claim_refuted'),
    ({'conditions':[{'state':'unknown','evidence_refs':['probe_1'],'condition':'Scope remains unknown.'}]},
     'semantic_condition_unresolved'),
    ({'conditions':[{'state':'unsatisfied','evidence_refs':['probe_1'],'condition':'Scope is excluded.'}]},
     'semantic_condition_unresolved'),
])
@pytest.mark.parametrize('malformed',[False,True])
def test_first_negative_or_unknown_never_gets_another_vote(monkeypatch,changes,code,malformed):
    calls=[]
    def handler(request):
        calls.append(1);value=verdict(**changes)
        if malformed:value['unknown_field']='Do not remove this restriction.'
        return response(value if len(calls)==1 else verdict())
    with pytest.raises(LightGraphError,match=code):
        review_semantics(client(monkeypatch,handler),PACKET,PROPOSITION,[])
    assert calls==[1]


@pytest.mark.parametrize('first_schema_error',[False,True])
def test_protocol_repair_is_at_most_one_call_across_transport_and_review(monkeypatch,first_schema_error):
    calls=[]
    def handler(request):
        body,packet=input_packet(request);calls.append(body)
        value=verdict(evidence=[{'reference':'not_issued','stance':'supports'}])
        if first_schema_error and len(calls)==1:
            value['conditions']=[{'state':'satisfied','evidence_refs':['probe_1']}]
        elif first_schema_error:
            value['conditions']=[{'state':'satisfied','evidence_refs':['probe_1'],'condition':'Allowed scope.'}]
        elif len(calls)==2:value['conditions']=[{'state':'satisfied','evidence_refs':['probe_1']}]
        assert 'tools' not in body
        return response(value)
    with pytest.raises(LightGraphError,match='semantic_review_unissued_evidence|llm_schema_invalid|semantic_review_patch_invalid'):
        review_semantics(client(monkeypatch,handler),PACKET,PROPOSITION,[])
    assert len(calls)==2


@pytest.mark.parametrize('drop_condition',[False,True])
def test_missing_compact_slot_cannot_unlock_existing_conditions(monkeypatch,drop_condition):
    calls=[]
    def handler(request):
        calls.append(1)
        conditions=[{'state':'satisfied','evidence_refs':['probe_1'],'condition':'Only within one namespace.'}]
        value=verdict(conditions=conditions)
        if len(calls)==1:del value['evidence'][0]['reference']
        elif drop_condition:value['conditions']=[]
        return response(value)
    model=client(monkeypatch,handler)
    if drop_condition:
        with pytest.raises(LightGraphError,match='semantic_claim_unresolved'):
            review_semantics(model,PACKET,PROPOSITION,[])
    else:assert review_semantics(model,PACKET,PROPOSITION,[])
    assert len(calls)==2


def test_every_document_still_requires_classification(monkeypatch):
    docs=[{'doc_id':'d','revision':'v1','section_id':str(i),'text':'Business definition',
        'evidence_role':'definition_evidence'} for i in (1,2)]
    calls=[]
    def handler(request):
        calls.append(1)
        return response(verdict(evidence=[{'reference':'d@v1:1','stance':'supports'}]) if len(calls)==1 else {})
    with pytest.raises(LightGraphError,match='semantic_review_incomplete_context'):
        review_semantics(client(monkeypatch,handler),PACKET,PROPOSITION,docs)
    assert len(calls)==2


@pytest.mark.parametrize('change',['fix_reference','delete','rewrite','change_stance'])
def test_outer_reference_patch_never_requests_a_second_verdict(monkeypatch,change):
    calls=[]
    def handler(request):
        body,packet=input_packet(request);calls.append(body)
        if len(calls)==1:
            return response(verdict(conditions=[{'state':'satisfied','evidence_refs':['not_issued'],
                'condition':'Only within the declared namespace.'}]))
        assert body['response_format']['json_schema']['schema']['title']=='SemanticReferencePatch'
        assert packet['semantic_review_repair']['condition_reference_uses']==[
            {'evidence_refs':['not_issued'],'condition':'Only within the declared namespace.'}]
        patch={'replacements':[{'invalid_reference':'not_issued','issued_reference':'probe_1'}]}
        if change=='delete':patch['conditions']=[]
        if change=='rewrite':patch['conditions']=[{'condition':'No restriction.'}]
        if change=='change_stance':patch['evidence']=[{'reference':'probe_1','stance':'context'}]
        return response(patch)
    model=client(monkeypatch,handler);model.config.retries=3
    if change=='fix_reference':assert review_semantics(model,PACKET,PROPOSITION,[])
    else:
        with pytest.raises(LightGraphError,match='semantic_review_patch_invalid'):
            review_semantics(model,PACKET,PROPOSITION,[])
    assert len(calls)==2


def test_higher_transport_retry_limit_does_not_allow_two_schema_corrections(monkeypatch):
    calls=[]
    def handler(request):
        calls.append(1);value=verdict()
        if len(calls)<3:del value['evidence'][0]['reference']
        return response(value)
    model=client(monkeypatch,handler);model.config.retries=3
    with pytest.raises(LightGraphError,match='llm_schema_invalid'):
        review_semantics(model,PACKET,PROPOSITION,[])
    assert len(calls)==2 and model.usage['schema_repair_attempts']==1


def test_transport_failure_does_not_consume_the_one_protocol_correction(monkeypatch):
    calls=[]
    def handler(request):
        calls.append(1)
        if len(calls)==1:return httpx.Response(503)
        return response(verdict(evidence=[{'reference':'not_issued','stance':'supports'}]) if len(calls)==2
            else {'replacements':[{'invalid_reference':'not_issued','issued_reference':'probe_1'}]})
    model=client(monkeypatch,handler)
    assert review_semantics(model,PACKET,PROPOSITION,[])
    assert len(calls)==3 and model.usage.get('schema_repair_responses',0)==0


def test_legacy_only_client_keeps_original_schema_and_exact_echo_checks():
    def complete(task,packet,schema):
        assert schema is SemanticReview and task=='semantic_review'
        return schema.model_validate(verdict(proposition=packet['semantic_proposition'],basis='data_inference'))
    assert review_semantics(SimpleNamespace(complete=complete),PACKET,PROPOSITION,[])==[
        'semantic_review:data_inference','probe_1']


def test_compact_condition_is_bounded_without_truncation_and_schema_is_smaller():
    value=verdict(conditions=[{'state':'satisfied','evidence_refs':['probe_1'],'condition':'x'*8192}])
    checked=BoundSemanticVerdict.model_validate(value)
    assert checked.conditions[0].condition==checked.bind('issued_ref').conditions[0].explanation=='x'*8192
    value['conditions'][0]['condition']+='x'
    with pytest.raises(ValidationError):BoundSemanticVerdict.model_validate(value)
    assert len(canonical(BoundSemanticVerdict.model_json_schema()))<len(canonical(SemanticReview.model_json_schema()))


def test_malformed_json_patch_is_not_retried(monkeypatch):
    calls=[]
    def handler(request):
        calls.append(1)
        if len(calls)==1:
            return response(verdict(evidence=[{'reference':'not_issued','stance':'supports'}]))
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"replacements":['},
            'finish_reason':'stop'}]})
    model=client(monkeypatch,handler);model.config.retries=3
    with pytest.raises(LightGraphError,match='semantic_review_patch_invalid'):
        review_semantics(model,PACKET,PROPOSITION,[])
    assert len(calls)==2 and not model.usage.get('schema_repair_attempts',0)


def test_invalid_reference_cannot_hide_a_later_unknown_condition(monkeypatch):
    calls=[]
    def handler(request):
        calls.append(1)
        return response(verdict(conditions=[
            {'state':'satisfied','evidence_refs':['not_issued'],'condition':'Declared namespace.'},
            {'state':'unknown','evidence_refs':['probe_1'],'condition':'Business scope is unknown.'}]))
    with pytest.raises(LightGraphError,match='semantic_condition_unresolved'):
        review_semantics(client(monkeypatch,handler),PACKET,PROPOSITION,[])
    assert len(calls)==1
