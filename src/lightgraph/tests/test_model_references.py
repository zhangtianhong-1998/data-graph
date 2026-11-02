"""Real model-boundary round trips; short spellings never change authority."""
import json
from copy import deepcopy

import pytest

from lightgraph.contracts import canonical
from lightgraph.discovery.model_references import ModelReferences
from lightgraph.discovery.semantics import BoundSemanticVerdict
from lightgraph.errors import LightGraphError
from lightgraph.tests.test_discovery_agent import client, response, input_packet, final, Toolbox


CODE='c_0123456789abcdef'
NAME='c_abcdef0123456789'
DOCUMENT={'doc_id':'document_definition_12345','revision':'revision_67890','section_id':'identity',
          'text':'原始文档中的 col1 和 '+CODE}
SECTION=DOCUMENT['doc_id']+'@'+DOCUMENT['revision']+':identity'


def packet():
    value=input_packet()
    value['metadata_digest']['columns']=[{'id':CODE,'name':'指标编码'}, {'id':NAME,'name':'中文名'}]
    value['strata_samples'][0]['rows']=[{CODE:'M01',NAME:'col1'}]
    return value


def test_typed_references_roundtrip_without_rewriting_business_values_or_sql():
    data=packet();data['document_snippets']=[DOCUMENT]
    original=deepcopy(data);refs=ModelReferences(data)
    shown=refs.encode(data)
    assert shown['metadata_digest']['columns'][0]['id']=='col1'
    assert shown['metadata_digest']['columns'][0]['name']=='指标编码'
    assert shown['strata_samples'][0]['rows']==[{'col1':'M01','col2':'col1'}]
    assert shown['document_snippets'][0]['reference']=='section1'
    assert shown['document_snippets'][0]['text']==DOCUMENT['text']
    proposal={'claim':{'members':[{'column_id':'col1','role':'identity_code'},
        {'column_id':'col2','role':'label'}], 'conditions':{'condition':'col1'},
        'property_names':{'col2':'display_label'}},'evidence_cited':['section1'],
        'label':'col1','description':'Use col1','sql':'SELECT col1','parameters':{'code':'col1'}}
    decoded=refs.decode(proposal)
    assert decoded['claim']['members'][0]['column_id']==CODE
    assert decoded['claim']['property_names']=={NAME:'display_label'}
    assert decoded['evidence_cited']==[SECTION]
    for field in ('label','description','sql','parameters'):
        assert decoded[field]==proposal[field]
    assert data==original
    refs.clear();assert not refs.forward and not refs.reverse


def test_position_mapping_schema_enums_and_unknown_aliases():
    refs=ModelReferences(packet())
    value={'positions':[{'code':CODE,'label':NAME,'aliases':[NAME]}]}
    assert refs.decode(refs.encode(value))==value
    schema={'properties':{'column_id':{'type':'string','enum':[CODE,NAME]},
                          'description':{'const':'ordinary value'}}}
    assert refs.schema(schema)['properties']['column_id']['enum']==['col1','col2']
    assert refs.decode({'column_id':'col999'})=={'column_id':'col999'}
    assert refs.decode({'column_id':'col01'})=={'column_id':'col01'}
    other=ModelReferences({'metadata_digest':{'columns':[{'id':NAME,'name':'中文名'}]}})
    assert other.reverse=={'col1':NAME}  # No cross-task registry or inherited authority.
    with pytest.raises(LightGraphError,match='model_reference_collision'):
        refs.decode({'property_names':{'col1':'label',CODE:'alias'}})


def test_source_tool_arguments_and_native_call_id_are_not_rewritten():
    refs=ModelReferences(packet())
    message={'tool_calls':[{'id':'provider-id','function':{'name':'execute_readonly_sql',
        'arguments':canonical({'sql':'SELECT col1','parameters':{'x':'col1'}})}}]}
    assert refs.response(message,{'submit_column_group'})==message


def test_short_canonical_ids_are_reserved_and_late_collisions_fail_closed():
    value={'objects':[{'object_id':'obj1'},{'object_id':'long_object_model_identifier'}]}
    refs=ModelReferences(value)
    assert refs.encode(value)['objects']==[{'object_id':'obj1'},{'object_id':'obj2'}]
    assert refs.decode(refs.encode(value))==value
    with pytest.raises(LightGraphError,match='model_reference_collision'):
        refs.observe({'object_id':'obj2'})
    refs=ModelReferences({'columns':[{'id':'col1','name':'Old'}, {'id':CODE,'name':'New'}]})
    assert refs.forward[CODE]=='col2'


def test_schema_business_literals_matching_an_id_remain_literal():
    refs=ModelReferences(packet())
    value={'properties':{'entity_kind_hint':{'enum':[CODE]},'description':{'const':CODE},
        'column_id':{'anyOf':[{'type':'string','enum':[CODE]},{'type':'null'}]}}}
    shown=refs.schema(value)
    assert shown['properties']['entity_kind_hint']==value['properties']['entity_kind_hint']
    assert shown['properties']['description']==value['properties']['description']
    assert shown['properties']['column_id']['anyOf'][0]['enum']==['col1']


def test_issued_reference_maps_keep_the_same_locator_as_their_catalog():
    data=packet()
    data['entity_bindings']=[{'rule_id':'long_entity_rule_identifier','key_columns':[CODE]}]
    data['authorized_entities']=['long_entity_rule_identifier']
    refs=ModelReferences(data)
    shown=refs.encode(data)
    assert shown['entity_bindings'][0]['rule_id']==shown['authorized_entities'][0]
    probe={'aggregates':{CODE:{'rows':12,'distinct_exact':3},'rows':12},
           'aggregate_columns':{'指标编码':CODE}}
    assert refs.encode(probe)=={'aggregates':{'col1':{'rows':12,'distinct_exact':3},'rows':12},
                                'aggregate_columns':{'指标编码':'col1'}}
    assert refs.decode(refs.encode(probe))==probe


def test_real_agent_roundtrip_restores_columns_before_tool_validation(monkeypatch):
    calls=[]
    def handler(request):
        body=json.loads(request.content);calls.append(body)
        visible=json.loads(body['messages'][1]['content'])
        assert visible['input']['metadata_digest']['columns'][0]['id']=='col1'
        if len(calls)==1:
            proposal=final()['proposals'][0]
            proposal.pop('kind')
            proposal['claim']['members'][0]['column_id']='col1'
            proposal['claim']['members'][1]['column_id']='col2'
            proposal['evidence_cited']=['col1']
            return response(tool='submit_column_group',args=proposal)
        return response(tool='finish_discovery',args={})
    model=client(monkeypatch,handler)
    result=model.explore(packet(),Toolbox(),max_steps=4)
    assert result.proposals[0].claim.members[0].column_id==CODE
    assert result.proposals[0].evidence_cited==[CODE]
    assert result._runtime_invalid_drafts==0
    assert calls[1]['messages'][-2]['tool_calls'][0]['id']=='provider-call'
    sent=json.loads(calls[1]['messages'][-2]['tool_calls'][0]['function']['arguments'])
    assert sent['claim']['members'][0]['column_id']=='col1'


def test_structured_review_resolves_short_sections_before_evidence_checks(monkeypatch):
    value=packet();value.update(document_snippets=[DOCUMENT],
        semantic_proposition_ref='proposition_0123456789abcdef',semantic_proposition={'kind':'test'},
        issued_evidence_refs=[SECTION],required_document_refs=[SECTION])
    def handler(request):
        body=json.loads(request.content)
        visible=json.loads(body['messages'][1]['content'])['input']
        assert visible['required_document_refs']==['section1']
        assert visible['document_snippets'][0]['reference']=='section1'
        return response({'decision':'supported','evidence':[{'reference':'section1','stance':'supports'}]})
    model=client(monkeypatch,handler)
    verdict=model.complete_bound_review(value,proposition_ref=value['semantic_proposition_ref'])
    assert verdict.proposition_ref==value['semantic_proposition_ref']
    assert verdict.evidence[0].reference==SECTION


def test_forged_document_alias_is_not_authorized(monkeypatch):
    value=packet();value['document_snippets']=[DOCUMENT]
    def handler(request):
        return response({'decision':'supported','evidence':[{'reference':'section99','stance':'supports'}]})
    verdict=client(monkeypatch,handler).complete('semantic_review',value,BoundSemanticVerdict)
    assert verdict.evidence[0].reference=='section99'  # Existing issued-reference check must reject it.
