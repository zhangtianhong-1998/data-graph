"""Independent review keeps full evidence once, with unchanged binding checks."""
from copy import deepcopy
import json

import pytest

from lightgraph.discovery.semantics import BoundSemanticVerdict, review_semantics
from lightgraph.errors import LightGraphError
from lightgraph.tests.test_agent_pipeline import client, input_packet, response
from lightgraph.tests.test_semantic_decisions import review_fixture


@pytest.mark.parametrize('task',['semantic_review','T4_document'])
def test_complete_context_references_full_input_without_duplicate_evidence(monkeypatch,task):
    docs,packet,proposition,_=review_fixture()
    docs[0]['text']='COMPLETE_DOCUMENT_CONDITION_83471'
    packet.update(document_snippets=docs,semantic_proposition=proposition,
        semantic_proposition_ref='bound_proposition_83471',
        required_document_refs=['context@1:rules'],
        strata_samples=[{'stratum':'sample','rows':[{'c_a':'SOURCE_OBSERVATION_83471'}]}],
        iteration_context={'pending_candidates':[{'reason':'RETAINED_COUNTEREVIDENCE_83471'}]})
    original=deepcopy(packet)
    model=client(monkeypatch,lambda request: pytest.fail('No provider call expected'))
    messages=model._complete_messages(task,packet,BoundSemanticVerdict)
    body=json.loads(messages[1]['content']);wire=body['input'];context=wire['call_context']
    for marker in ('COMPLETE_DOCUMENT_CONDITION_83471','SOURCE_OBSERVATION_83471',
                   'RETAINED_COUNTEREVIDENCE_83471'):
        assert messages[1]['content'].count(marker)==1
    for field,target in (('document_snippets','document_snippets'),
                         ('observations','strata_samples'),('iteration_context','iteration_context')):
        assert field not in context
        assert context[field+'_ref']=='input.'+target
        assert wire[target]
    assert wire['document_snippets']==docs
    assert wire['semantic_proposition']==proposition
    assert wire['semantic_proposition_ref']==packet['semantic_proposition_ref']
    assert wire['required_document_refs']==packet['required_document_refs']
    assert packet==original


@pytest.mark.parametrize('decision,stance',[('supported','supports'),('refuted','refutes')])
def test_bound_review_preserves_full_document_and_negative_decision(monkeypatch,decision,stance):
    docs,packet,proposition,_=review_fixture()
    docs[0]['text']='MANDATORY_REVIEW_CONDITION_83471'
    packet['iteration_context']={'counterevidence':'ORIGINAL_OBJECTION_83471'}
    calls=[]
    def handler(request):
        body,wire=input_packet(request);calls.append(wire)
        encoded=json.dumps(body,ensure_ascii=False)
        assert encoded.count('MANDATORY_REVIEW_CONDITION_83471')==1
        assert encoded.count('ORIGINAL_OBJECTION_83471')==1
        assert wire['semantic_proposition']==proposition
        assert wire['semantic_proposition_ref']=='claim1'
        assert len(wire['required_document_refs'])==1
        assert set(wire['required_document_refs'])<=set(wire['issued_evidence_refs'])
        return response({'decision':decision,'evidence':[
            {'reference':ref,'stance':stance} for ref in wire['required_document_refs']],
            'conditions':[]})
    model=client(monkeypatch,handler)
    if decision=='refuted':
        with pytest.raises(LightGraphError,match='semantic_claim_refuted'):
            review_semantics(model,packet,proposition,docs)
    else:
        refs=review_semantics(model,packet,proposition,docs)
        assert 'context@1:rules' in refs
    assert len(calls)==1
