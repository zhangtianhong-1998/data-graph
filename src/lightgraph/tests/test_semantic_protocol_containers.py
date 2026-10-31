"""Protocol normalization preserves all verdicts, conditions and evidence."""
import pytest

from lightgraph.contracts import canonical
from lightgraph.discovery.semantics import review_semantics
from lightgraph.errors import LightGraphError
from lightgraph.tests.test_agent_pipeline import client, response
from lightgraph.tests.test_bound_semantic_verdict import PACKET, PROPOSITION, verdict


def test_singleton_containers_are_lossless_and_need_no_second_judgment(monkeypatch):
    calls=[]
    def handler(request):
        calls.append(1)
        return response(verdict(
            evidence={'reference':'probe_1','stance':'supports'},
            conditions={'state':'satisfied','evidence_refs':'probe_1',
                        'condition':'Only in the declared namespace.'}))
    model=client(monkeypatch,handler)
    assert review_semantics(model,PACKET,PROPOSITION,[]) == [
        'semantic_review:bound_model_review','probe_1']
    assert calls == [1]
    assert model.usage['semantic_review_container_normalizations']==3
    assert not model.usage.get('schema_repair_attempts')


@pytest.mark.parametrize('change,code',[
    ({'decision':'refuted'},'semantic_claim_refuted'),
    ({'decision':'unresolved'},'semantic_claim_unresolved'),
    ({'evidence':{'reference':'probe_1','stance':'refutes'}},'semantic_claim_refuted'),
    ({'conditions':{'state':'unknown','evidence_refs':'probe_1',
                    'condition':'Applicability is unknown.'}},'semantic_condition_unresolved'),
])
def test_singleton_normalization_never_erases_negative_or_unknown(monkeypatch,change,code):
    calls=[]
    def handler(request):
        calls.append(1)
        value=verdict(evidence={'reference':'probe_1','stance':'supports'})
        value.update(change)
        return response(value if len(calls)==1 else verdict())
    with pytest.raises(LightGraphError,match=code):
        review_semantics(client(monkeypatch,handler),PACKET,PROPOSITION,[])
    assert calls==[1]


def test_singleton_schema_repair_fills_only_missing_locator(monkeypatch):
    calls=[]
    def handler(request):
        calls.append(1)
        item={'stance':'supports'}
        if len(calls)>1:item['reference']='probe_1'
        return response(verdict(evidence=item))
    model=client(monkeypatch,handler)
    assert review_semantics(model,PACKET,PROPOSITION,[])
    assert calls==[1,1] and model.usage['schema_repairs_succeeded']==1
    diagnostics=model.usage['semantic_review_protocol_diagnostics']
    assert diagnostics=={canonical({'code':'missing','field':['evidence',0,'reference']}):1}


def test_protocol_diagnostics_never_retain_unknown_field_or_input(monkeypatch):
    canary='PRIVATE_REVIEW_CONTENT_0247'
    calls=[]
    def handler(request):
        calls.append(1)
        return response(verdict(evidence={'reference':'probe_1','stance':'supports',canary:canary}))
    model=client(monkeypatch,handler)
    with pytest.raises(LightGraphError,match='semantic_review_protocol_invalid'):
        review_semantics(model,PACKET,PROPOSITION,[])
    assert len(calls)==2
    assert canary not in canonical(model.usage)
    assert '<unknown_field>' in canonical(model.usage['semantic_review_protocol_diagnostics'])


def test_singleton_unissued_reference_still_needs_reference_patch(monkeypatch):
    calls=[]
    def handler(request):
        calls.append(1)
        return response(verdict(evidence={'reference':'not_issued','stance':'supports'})
            if len(calls)==1 else {'replacements':[{
                'invalid_reference':'not_issued','issued_reference':'probe_1'}]})
    model=client(monkeypatch,handler)
    assert review_semantics(model,PACKET,PROPOSITION,[])
    assert len(calls)==2 and model.usage['semantic_reference_repairs_succeeded']==1
