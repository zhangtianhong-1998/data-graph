"""Explicit hierarchy policies repair locally without changing identity meaning."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from lightgraph.contracts import ExtractionGoal
from lightgraph.discovery import discover
from lightgraph.discovery.engine import _column_id, _compile_model, _source
from lightgraph.discovery.models import DiscoveryResult, ProposalBatch, TaskPacket
from lightgraph.discovery.observations import ReviewObservations
from lightgraph.discovery.semantics import hierarchy_identity_context, entity_granularity_context_signatures
from lightgraph.discovery.workflow import _compile, _compile_candidates, _proposal_repair_record
from lightgraph.errors import LightGraphError
from lightgraph.rules import RuleEngine
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import client, group, input_packet, response, table


UNKNOWN='UNRETAINED_IDENTITY_POLICY_6318'
OMITTED=object()


def fixture(source_id='s'):
    source=table('objects',['tenant','head','head_name','leaf','leaf_name','aux','aux_label'])
    source.source_id=source_id
    packet={'task':'T0_discovery','scope':_source(source).model_dump(mode='json',by_alias=True),
        'metadata_digest':{'columns':[{'name':c.name,'id':_column_id(source,c.name)} for c in source.columns]}}
    return source,packet


def hierarchy(packet, policy=OMITTED, *, path='relative_path', scope='namespace_local'):
    ids={c['name']:c['id'] for c in packet['metadata_digest']['columns']}
    claim={'group_id':'tree','entity_kind_hint':'ReportItem','namespace_columns':[ids['tenant']],
        'levels':[{'position':1,'key':ids['head'],'label':ids['head_name']},
                  {'position':2,'key':ids['leaf'],'label':ids['leaf_name']}],
        'path_semantics':path,'identity_scope':scope,'ordering_evidence':[ids['head']]}
    if path=='relative_path':
        claim.update(identity_domain='objects',taxonomy='paths',position_semantics='ordinal_slots')
    if policy is not OMITTED:claim['identity_resolution']=policy
    return {'kind':'wide_hierarchy','claim':claim,'evidence_cited':[ids['head']]}


class Reviewer:
    def __init__(self, *, decision='supported'):
        self.decision=decision;self.review_packets=[];self.explorations=0
        self.config=SimpleNamespace(max_discovery_steps=4,max_tool_calls=3)

    def complete(self,task,packet,contract):
        assert task=='semantic_review'
        self.review_packets.append(copy.deepcopy(packet))
        return contract.model_validate({'proposition_ref':packet['semantic_proposition_ref'],
            'decision':self.decision,'basis':'data_inference','evidence':[{
                'reference':packet['metadata_digest']['columns'][1]['id'],
                'stance':'refutes' if self.decision=='refuted' else 'supports'}]})

    def explore(self,packet,tools,**kwargs):
        self.explorations+=1
        raise AssertionError('Malformed policies must not start another Agent')


INVALID=[
    (UNKNOWN,'relative_path','namespace_local','hierarchy_identity_resolution_unsupported'),
    ('shared_definition','relative_path','namespace_local','hierarchy_identity_resolution_unsupported'),
    ('namespace_local','relative_path','namespace_local','hierarchy_identity_resolution_unsupported'),
    ('','legacy','namespace_local','hierarchy_identity_resolution_unsupported'),
    ('source_scoped','relative_path','namespace_local','hierarchy_identity_resolution_conflict'),
    ('verified_identity_domain','legacy','namespace_local','hierarchy_identity_resolution_conflict'),
    ('source_scoped','legacy','domain_global','hierarchy_identity_resolution_conflict'),
    (None,'legacy','domain_global','hierarchy_identity_resolution_conflict'),
]


@pytest.mark.parametrize('policy,path,scope,code',INVALID)
def test_invalid_policy_is_local_contract_failure_before_paid_review(policy,path,scope,code):
    source,packet=fixture();bad=hierarchy(packet,policy,path=path,scope=scope)
    value={'task':'T0_discovery','proposals':[group(packet,'aux','aux_label')['proposals'][0],bad]}
    # Unknown semantic values remain typed candidates, not whole-batch failure.
    assert Draft202012Validator(ProposalBatch.model_json_schema()).is_valid(value)
    batch=ProposalBatch.model_validate(value)
    result=DiscoveryResult();failures=[];reviewer=Reviewer()
    rules=_compile(batch,source,ExtractionGoal(),[],result,model=reviewer,packet=packet,failures=failures)
    assert [r.kind for r in rules]==['column_group_entity']
    assert [(p.kind,c) for p,c in failures]==[('wide_hierarchy',code)]
    assert not reviewer.review_packets and not result.issues
    with pytest.raises(LightGraphError) as caught:
        _compile_model(ProposalBatch(task='T0_discovery',proposals=[bad]),source,ExtractionGoal(),[],[],
            semantic_evidence=['review:approved'])
    assert caught.value.code==code  # Compiler defense also rejects direct callers.


@pytest.mark.parametrize('policy,path',[(None,'legacy'),(None,'relative_path'),
    ('source_scoped','legacy'),('verified_identity_domain','relative_path')])
@pytest.mark.parametrize('decision',['supported','refuted'])
def test_consistent_policies_and_defaults_still_need_exact_semantic_review(policy,path,decision):
    source,packet=fixture();reviewer=Reviewer(decision=decision);result=DiscoveryResult();failures=[]
    rules=_compile(ProposalBatch(task='T0_discovery',proposals=[hierarchy(packet,policy,path=path)]),
        source,ExtractionGoal(),[],result,model=reviewer,packet=packet,failures=failures)
    assert len(reviewer.review_packets)==1 and not failures
    assert reviewer.review_packets[0]['semantic_proposition']['claim']['identity_resolution']==policy
    if decision=='supported':
        assert len(rules)==1 and not result.issues
        assert rules[0].identity.semantic_resolution==(
            'verified_identity_domain' if path=='relative_path' else 'source_scoped')
    else:
        assert not rules
        assert [issue['code'] for issue in result.issues]==['semantic_claim_refuted']


@pytest.mark.parametrize('policy,path,node_count,edge_count',[
    ('source_scoped','legacy',4,2),('verified_identity_domain','relative_path',2,1)])
def test_source_scoped_and_namespace_local_have_different_cross_source_identity(policy,path,node_count,edge_count):
    reviewer=Reviewer()
    with GraphStore() as store:
        for source_id in ('one','two'):
            source,packet=fixture(source_id);result=DiscoveryResult()
            rules=_compile(ProposalBatch(task='T0_discovery',proposals=[hierarchy(packet,policy,path=path)]),
                source,ExtractionGoal(),[],result,model=reviewer,packet=packet)
            assert len(rules)==1 and not result.issues
            with MemorySource([source],{'public.objects':[('t','A','Alpha','B','Beta','X','Aux')]}) as adapter:
                outcome=RuleEngine(store).execute(adapter,rules[0])
                assert outcome.published,outcome.codes
        graph=store.view({'t'})
        assert len(graph['nodes'])==node_count and len(graph['edges'])==edge_count
    assert len(reviewer.review_packets)==2


def run_candidates(initial,source,packet, *, decision='supported'):
    reviewer=Reviewer(decision=decision);result=DiscoveryResult()
    with ReviewObservations() as observations:
        rules,*_=_compile_candidates(ProposalBatch(task='T0_discovery',proposals=initial),
            source,ExtractionGoal(),[],result,model=reviewer,packet=packet,observations=observations)
    assert reviewer.explorations==0
    return rules,result,reviewer


def policy_request(packet,policy=UNKNOWN,code='hierarchy_identity_resolution_unsupported'):
    proposal=ProposalBatch(task='T0_discovery',proposals=[hierarchy(packet,policy)]).proposals[0]
    return _proposal_repair_record(proposal,code)


def test_unsupported_policy_stays_deferred_and_keeps_valid_sibling():
    source,packet=fixture()
    rules,result,reviewer=run_candidates([hierarchy(packet,UNKNOWN),
        group(packet,'aux','aux_label')['proposals'][0]],source,packet)
    assert [rule.kind for rule in rules]==['column_group_entity']
    assert not reviewer.review_packets
    assert any(issue['code']=='hierarchy_identity_resolution_unsupported' and issue['status']=='deferred'
        for issue in result.issues)
    assert UNKNOWN not in json.dumps(result.model_dump())


def test_semantically_supported_policy_does_not_bypass_full_data_consistency():
    source,packet=fixture();result=DiscoveryResult()
    rules=_compile(ProposalBatch(task='T0_discovery',proposals=[
        hierarchy(packet,'verified_identity_domain')]),source,ExtractionGoal(),[],result,
        model=Reviewer(),packet=packet)
    assert len(rules)==1 and not result.issues
    with MemorySource([source],{'public.objects':[
        ('t','A','Alpha','B','Beta','X','Aux'),('t','A','Changed','B','Beta','X','Aux')]}) as adapter,GraphStore() as store:
        outcome=RuleEngine(store).execute(adapter,rules[0])
        assert not outcome.published and 'same_code_diff_label' in outcome.codes
        assert not store.view({'t'})['nodes']



def test_later_fd_task_cannot_reset_explicit_policy_requirement():
    source,packet=fixture()
    packet.update(hierarchy_identity_requests=[policy_request(packet)],allowed_revision_codes=['fd_violation'])
    result=DiscoveryResult();failures=[];reviewer=Reviewer()
    rules=_compile(ProposalBatch(task='T5_revision',proposals=[hierarchy(packet)]),
        source,ExtractionGoal(),[],result,model=reviewer,packet=packet,failures=failures)
    assert not rules and not reviewer.review_packets
    assert [code for _,code in failures]==['hierarchy_identity_resolution_required']


def test_bounded_identity_context_preserves_valid_sibling():
    source,packet=fixture();requests=[]
    for index in range(12):
        request=policy_request(packet);request['proposal']['claim']['group_id']=f'prior_{index}'
        requests.append(request)
    packet['hierarchy_identity_requests']=requests
    TaskPacket.model_validate(packet)
    rules,result,reviewer=run_candidates([hierarchy(packet,'source_scoped'),
        group(packet,'aux','aux_label')['proposals'][0]],source,packet)
    assert [rule.kind for rule in rules]==['column_group_entity']
    assert not reviewer.review_packets
    assert any(issue['code']=='hierarchy_identity_repair_context_limit' and issue['status']=='deferred'
        for issue in result.issues)
    assert UNKNOWN not in json.dumps(result.model_dump())


def test_real_task_packet_reports_unsupported_policy_without_extra_call(monkeypatch):
    source,_=fixture();tasks=[]
    def handler(request):
        _,packet=input_packet(request);tasks.append(packet['task'])
        assert packet['task']=='T0_discovery'
        return response({'task':packet['task'],'proposals':[
            hierarchy(packet,UNKNOWN),*group(packet,'aux','aux_label')['proposals']]})
    model=client(monkeypatch,handler)
    with MemorySource([source],{'public.objects':[('t','A','Alpha','B','Beta','X','Aux')]}) as adapter:
        result=discover(adapter,model=model)
    assert [rule.kind for rule in result.rules]==['column_group_entity']
    assert tasks==['T0_discovery']
    assert any(issue['code']=='hierarchy_identity_resolution_unsupported' for issue in result.issues)
    assert UNKNOWN not in json.dumps(result.model_dump())


def test_grain_signature_uses_effective_policy_and_preserves_explicit_conflict():
    source,packet=fixture()
    def signature(policy,path):
        batch=ProposalBatch(task='T0_discovery',proposals=[hierarchy(packet,policy,path=path)])
        return entity_granularity_context_signatures({'hierarchies':hierarchy_identity_context(batch,source)})
    for path,policy in [('legacy','source_scoped'),('relative_path','verified_identity_domain')]:
        assert signature(OMITTED,path)==signature(None,path)==signature(policy,path)
        assert signature(policy,path)!=signature(UNKNOWN,path)
    assert signature('source_scoped','relative_path')!=signature('verified_identity_domain','relative_path')



def test_task_packet_schema_parity_keeps_conflict_history_typed_and_bounded():
    schema=TaskPacket.model_json_schema()
    persisted=Path(__file__).resolve().parents[1]/'schemas/TaskPacket.schema.json'
    assert json.loads(persisted.read_text())==schema
    assert schema['properties']['hierarchy_identity_requests']['maxItems']==12
    _,packet=fixture();packet['hierarchy_identity_requests']=[policy_request(packet)]
    assert Draft202012Validator(schema).is_valid(packet)
    assert not Draft202012Validator(schema).is_valid({**packet,
        'hierarchy_identity_requests':packet['hierarchy_identity_requests']*13})


@pytest.mark.parametrize('reverse',[False,True])
@pytest.mark.parametrize('removed',[OMITTED,None])
def test_same_batch_default_sibling_cannot_erase_policy_conflict(reverse,removed):
    source,packet=fixture();renamed=hierarchy(packet,removed)
    renamed['claim']['group_id']='renamed'
    values=[hierarchy(packet,'source_scoped'),renamed]
    if reverse:values.reverse()
    values+=group(packet,'aux','aux_label')['proposals']
    rules,result,reviewer=run_candidates(values,source,packet)
    assert [rule.kind for rule in rules]==['column_group_entity']
    assert not reviewer.review_packets
    assert {item['code'] for item in result.issues}=={
        'hierarchy_identity_resolution_conflict','hierarchy_identity_resolution_required'}


@pytest.mark.parametrize('reverse',[False,True])
def test_same_batch_explicit_alternative_is_reviewed_with_original_conflict(reverse):
    source,packet=fixture()
    proposals=[hierarchy(packet,'verified_identity_domain'),hierarchy(packet,'source_scoped')]
    if reverse:proposals.reverse()
    rules,result,reviewer=run_candidates(proposals,source,packet)
    assert [rule.kind for rule in rules]==['wide_hierarchy'] and not result.issues
    assert len(reviewer.review_packets)==1
    assert reviewer.review_packets[0]['hierarchy_identity_requests'][0]['proposal']['claim'][
        'identity_resolution']=='source_scoped'


def test_independent_hierarchy_keeps_compatible_default_despite_bad_policy():
    source,packet=fixture();other=hierarchy(packet)
    ids={c['name']:c['id'] for c in packet['metadata_digest']['columns']}
    other['claim'].update(group_id='independent',levels=[
        {'position':1,'key':ids['aux']},{'position':2,'key':ids['aux_label']}])
    rules,result,reviewer=run_candidates([hierarchy(packet,'source_scoped'),other],source,packet)
    assert len(rules)==1 and rules[0].hierarchy.levels[0].code=='aux'
    assert len(reviewer.review_packets)==1
    assert not reviewer.review_packets[0]['hierarchy_identity_requests']
    assert any(issue['code']=='hierarchy_identity_resolution_conflict' for issue in result.issues)
