"""Object mappings can be enriched without losing identities or guarded work."""
from copy import deepcopy
import json

from lightgraph.discovery.proposal_session import ProposalSession
from lightgraph.discovery.hierarchy_plan import compile_hierarchy_plan
from lightgraph.discovery.engine import _compile_model
from lightgraph.contracts import ExtractionGoal
from lightgraph.tests.test_discovery_agent import input_packet
from lightgraph.tests.test_proposal_session import group
from lightgraph.tests.test_hierarchy_plan import fixture
from lightgraph.tests.test_submission_reference_feedback import packet,shared


def test_object_model_is_candidate_and_latest_mapping_stays_in_packet():
    p=input_packet();session=ProposalSession(p)
    ids=[c['id'] for c in p['metadata_digest']['columns']]
    first={'object_id':'item','key_columns':[ids[0]],'label_columns':[ids[1]]}
    assert session.execute('declare_object_model',first)['candidate']
    second={**first,'label_columns':[],'english_name_columns':[ids[1]]}
    assert session.execute('declare_object_model',second)['status']=='ok'
    models=session.batch()._runtime_object_models
    assert len(models)==2
    assert p['schema_knowledge']['objects'][0]['english_name_columns']==[ids[1]]
    assert p['schema_knowledge']['objects'][0]['status']=='candidate'
    assert session.batch().abstain and not session.batch().proposals
    assert '_runtime_object_models' not in session.batch().model_dump_json()
    bad={**first,'key_columns':['PRIVATE_COLUMN']}
    rejected=session.execute('declare_object_model',bad)
    assert rejected['code']=='unauthorized_column_reference'
    assert rejected['diagnostics'][0]['field']==['key_columns',0]
    assert 'PRIVATE_COLUMN' not in json.dumps(rejected)
    assert len(session.batch()._runtime_object_models)==2
    session.clear();assert not session.object_models


def test_object_model_survives_legacy_final_runtime_and_final_validation():
    from lightgraph.tests.test_model_streaming import model
    from lightgraph.tests.test_discovery_agent import Toolbox,final,response
    from lightgraph.tests.test_proposal_session import tools_response
    p=input_packet();ids=[c['id'] for c in p['metadata_digest']['columns']];calls=[]
    def handler(request):
        body=json.loads(request.content);calls.append(body)
        if len(calls)==1:
            assert any(t['function']['name']=='declare_object_model' for t in body['tools'])
            return tools_response(('declare_object_model',{'object_id':'item','key_columns':[ids[0]],
                'label_columns':[ids[1]]}))
        envelope=json.loads(body['messages'][1]['content'])
        assert envelope['input']['schema_knowledge']['objects'][0]['status']=='candidate'
        return response(final())
    client=model(handler,stream=False)
    batch=client.explore(p,Toolbox(),max_steps=2)
    assert len(batch.proposals)==1 and len(batch._runtime_object_models)==1
    assert batch._runtime_object_models[0]['label_columns']==[ids[1]]


def test_object_model_survives_truncation_without_an_entity_draft():
    import httpx
    from lightgraph.tests.test_model_streaming import model
    from lightgraph.tests.test_discovery_agent import Toolbox
    from lightgraph.tests.test_proposal_session import tools_response
    p=input_packet();column=p['metadata_digest']['columns'][0]['id'];calls=[]
    def handler(request):
        calls.append(request)
        if len(calls)==1:
            return tools_response(('declare_object_model',{'object_id':'item','key_columns':[column]}))
        return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'unfinished'},
            'finish_reason':'length'}]})
    batch=model(handler,stream=False).explore(p,Toolbox(),max_steps=2)
    assert batch.abstain and len(batch._runtime_object_models)==1
    assert 'model_response_truncated' in batch._runtime_pending_codes
    assert 'discovery_submission_incomplete' in batch._runtime_pending_codes


def test_explicit_display_repair_replaces_one_draft_without_conflicting_sibling():
    session=ProposalSession(input_packet());value=group()
    old=session.execute('submit_column_group',value)
    revised=deepcopy(value)
    revised['claim']['members'][-1]['role']='alias'
    rejected=session.execute('submit_column_group',revised)
    assert rejected['code']=='draft_replacement_required'
    assert rejected['replace_draft_id']==old['draft_id']
    revised['replace_draft_id']=old['draft_id']
    revised['resolves_rejections']=[rejected['rejection_id']]
    result=session.execute('submit_column_group',revised)
    assert result=={'status':'ok','draft_id':'draft_1','replaced':True,'confirmed':False}
    assert len(session.batch().proposals)==1
    assert session.batch().proposals[0].claim.members[-1].role=='alias'
    assert not session.open_rejections


def test_failed_or_wrong_target_replacement_preserves_previous_draft():
    session=ProposalSession(input_packet());value=group()
    old=session.execute('submit_column_group',value);before=session.batch().model_dump()
    revised=deepcopy(value);revised['replace_draft_id']=old['draft_id']
    revised['claim']['group_id']='unrelated'
    assert session.execute('submit_column_group',revised)['code']=='invalid_draft_replacement'
    revised['claim']['group_id']=value['claim']['group_id']
    revised['claim']['members'][0]['column_id']='private_invalid_column'
    assert session.execute('submit_column_group',revised)['code']=='unauthorized_column_reference'
    assert session.batch().model_dump()==before


def test_replacement_cannot_erase_identity_exclusion_or_evidence_obligations():
    session=ProposalSession(input_packet());value=group()
    value['claim']['members'][-1].update(role='audit',exclude_from_projection=True)
    old=session.execute('submit_column_group',value)
    revised=deepcopy(value);revised['replace_draft_id']=old['draft_id']
    revised['claim']['members'][-1].update(role='label',exclude_from_projection=False)
    assert session.execute('submit_column_group',revised)['code']=='draft_replacement_changes_obligations'
    revised=deepcopy(value);revised['replace_draft_id']=old['draft_id']
    revised['claim']['members'][0]['in_identity']=False
    assert session.execute('submit_column_group',revised)['code']=='draft_replacement_changes_obligations'
    assert session.batch().proposals[0].claim.members[-1].exclude_from_projection


def test_hierarchy_replacement_keeps_plan_target_and_topology_with_multiple_aliases():
    table,p,plan=fixture();session=ProposalSession(p)
    old=session.execute('submit_hierarchy_plan',plan)
    revised=deepcopy(plan);revised['replace_draft_id']=old['draft_id']
    # Repeated mapping is deduplicated; multiple issued aliases compile together.
    revised['positions'][0]['aliases']=[plan['positions'][0]['alias'],plan['positions'][1]['alias']]
    assert session.execute('submit_hierarchy_plan',revised)['replaced']
    rule=_compile_model(session.batch(),table,ExtractionGoal(),[],[],
        semantic_evidence=['semantic_review:data_inference','probe_1'])[0]
    assert rule.hierarchy.levels[0].aliases==['a1','a2']
    revised['positions'][0]['code']=plan['positions'][1]['code']
    assert session.execute('submit_hierarchy_plan',revised)['code']=='draft_replacement_changes_obligations'
    assert len(session.batch().proposals)==1


def test_field_diagnostics_preserve_each_reference_failure_category():
    session=ProposalSession(packet());value=shared('invalid_document')
    value['claim']['members'][0]['column_id']='invalid_column'
    response=session.execute('submit_column_group',value)
    assert response['code']=='identity_domain_evidence_unresolved'
    details=session.batch()._runtime_invalid_diagnostics
    assert {d['code'] for d in details}=={'identity_domain_evidence_unresolved','unauthorized_column_reference'}
    assert next(d for d in details if d['code']=='unauthorized_column_reference')['path']==['claim','members',0,'column_id']
    assert 'invalid_column' not in json.dumps(details) and 'invalid_document' not in json.dumps(details)
