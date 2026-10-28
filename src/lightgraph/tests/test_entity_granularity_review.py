"""Composite hierarchy-position keys need evidence for their business grain."""
from types import SimpleNamespace

import pytest

from lightgraph.contracts import ExtractionGoal
from lightgraph.discovery import discover
from lightgraph.discovery.engine import _column_id, _source
from lightgraph.discovery.models import DiscoveryResult, ProposalBatch
from lightgraph.discovery.observations import ReviewObservations
from lightgraph.discovery.workflow import _compile, _compile_with_repairs
from lightgraph.rules import RuleEngine
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import client, input_packet, response, table


def fixture():
    source=table('catalog',['domain','head','leaf','caption','aux','aux_label'])
    goal=ExtractionGoal(entity_kinds=['CatalogObject'],
        exclude=['Do not treat each path row as an independent business object.'])
    packet={'task':'T0_discovery','scope':_source(source).model_dump(mode='json',by_alias=True),
        'goal':goal.model_dump(mode='json'),
        'metadata_digest':{'columns':[{'name':c.name,'id':_column_id(source,c.name)} for c in source.columns]}}
    return source,goal,packet


def hierarchy(packet):
    ids={c['name']:c['id'] for c in packet['metadata_digest']['columns']}
    return {'kind':'wide_hierarchy','claim':{'group_id':'tree','entity_kind_hint':'CatalogObject',
        'levels':[{'position':1,'key':ids['head']},{'position':2,'key':ids['leaf']}],
        'path_semantics':'relative_path','identity_domain':'catalog_objects','taxonomy':'catalog_paths',
        'position_semantics':'ordinal_slots','ordering_evidence':[ids['head']]},
        'evidence_cited':[ids['head']]}


def composite(packet,kind='column_group',keys=('head','leaf'),*,extra=False):
    ids={c['name']:c['id'] for c in packet['metadata_digest']['columns']}
    claim={'group_id':'object','entity_kind_hint':'CatalogObject'}
    if kind=='column_group':
        claim['members']=[{'column_id':ids[name],'role':'identity_code','in_identity':True} for name in keys]
        claim['members'].append({'column_id':ids['caption'],'role':'label'})
        if extra:
            claim['members'].append({'column_id':ids['aux'],'role':'semantic_attribute','property_name':'custom_state'})
    else:
        claim.update(key_columns=[ids[name] for name in keys],attributes={
            'single_valued':[ids['caption']],'property_names':{ids['caption']:'label'}})
    return {'kind':kind,'claim':claim,'evidence_cited':[ids[keys[0]]]}


class Reviewer:
    def __init__(self,decision='supported',repairs=()):
        self.decision=decision;self.reviews=[];self.repairs=list(repairs);self.explorations=0
        self.config=SimpleNamespace(max_discovery_steps=4,max_tool_calls=3)

    def complete(self,task,packet,contract):
        proposition=packet['semantic_proposition']
        self.reviews.append(proposition)
        grain=proposition['kind']=='entity_granularity'
        if grain:
            assert packet['goal']['exclude']
            assert 'FD success' in packet['subtask']
            assert proposition['candidate']['claim']
            assert proposition['hierarchy_identity_context'][0]['identity_domain']=='catalog_objects'
        decision=self.decision if grain else 'supported'
        return contract.model_validate({'proposition_ref':packet['semantic_proposition_ref'],
            'decision':decision,'basis':'data_inference','evidence':[{
                'reference':packet['metadata_digest']['columns'][1]['id'],
                'stance':'refutes' if decision=='refuted' else 'context' if decision=='unresolved' else 'supports'}]})

    def explore(self,packet,tools,**kwargs):
        self.explorations+=1
        return ProposalBatch.model_validate({'task':packet['task'],'proposals':self.repairs.pop(0)})


@pytest.mark.parametrize('kind',['column_group','entity_key'])
@pytest.mark.parametrize('reverse',[False,True])
@pytest.mark.parametrize('decision',['refuted','unresolved'])
def test_path_identity_rejected_independently_of_proposal_order(kind,reverse,decision):
    source,goal,packet=fixture()
    proposals=[composite(packet,kind),hierarchy(packet)]
    if reverse:proposals.reverse()
    reviewer=Reviewer(decision);result=DiscoveryResult();failures=[]
    rules=_compile(ProposalBatch(task='T0_discovery',proposals=proposals),source,goal,[],result,
        model=reviewer,packet=packet,failures=failures)
    assert [rule.kind for rule in rules]==['wide_hierarchy']
    assert not failures  # Semantic refutation/uncertainty is not contract repair.
    assert [i['code'] for i in result.issues]==['semantic_claim_'+decision]
    assert sum(p['kind']=='entity_granularity' for p in reviewer.reviews)==1


@pytest.mark.parametrize('kind',['column_group','entity_key'])
def test_evidenced_business_composite_key_survives_and_persists_review(kind):
    source,goal,packet=fixture();reviewer=Reviewer();result=DiscoveryResult()
    rules=_compile(ProposalBatch(task='T0_discovery',proposals=[composite(packet,kind),hierarchy(packet)]),
        source,goal,[],result,model=reviewer,packet=packet)
    assert not result.issues and len(rules)==2
    group=next(rule for rule in rules if rule.kind=='column_group_entity')
    assert group.identity.key_columns==['head','leaf']
    assert group.evidence.semantic_decision=='model_review'
    assert group.evidence.semantic_status=='supported' and group.evidence.conditions_satisfied
    assert 'semantic_review:entity_granularity' in group.evidence.semantic_basis
    with MemorySource([source],{'public.catalog':[('D','A','B','Object','X','X')]}) as adapter,GraphStore() as store:
        assert RuleEngine(store).execute(adapter,group).published
        assert 'semantic_review:entity_granularity' in store.get_rules()[0].evidence.semantic_basis


def test_single_position_with_namespace_is_not_mistaken_for_path_grain():
    source,goal,packet=fixture();reviewer=Reviewer('refuted');result=DiscoveryResult()
    value=composite(packet,keys=('domain','head'))
    rules=_compile(ProposalBatch(task='T0_discovery',proposals=[value,hierarchy(packet)]),source,goal,[],result,
        model=reviewer,packet=packet)
    assert len(rules)==2 and not result.issues
    assert [p['kind'] for p in reviewer.reviews]==['wide_hierarchy']


def test_repair_retains_compiled_hierarchy_and_does_not_resolicit_refutation():
    source,goal,packet=fixture();bad=composite(packet,keys=('aux',),extra=True)
    rejected=composite(packet)
    changed_id=composite(packet);changed_id['claim']['group_id']='renamed'
    reviewer=Reviewer('refuted',repairs=[[changed_id,bad]])
    result=DiscoveryResult()
    tools=lambda:SimpleNamespace(context_documents=lambda:[],close=lambda:None)
    with ReviewObservations() as observations:
        rules,*_=_compile_with_repairs(ProposalBatch(task='T0_discovery',
            proposals=[hierarchy(packet),rejected,bad]),source,goal,[],result,
            model=reviewer,packet=packet,observations=observations,tool_factory=tools)
    assert [rule.kind for rule in rules]==['wide_hierarchy']
    assert reviewer.explorations==1
    assert sum(p['kind']=='entity_granularity' for p in reviewer.reviews)==1
    assert any(i['code']=='semantic_claim_refuted' for i in result.issues)


@pytest.mark.parametrize('decision',['refuted','unresolved'])
@pytest.mark.parametrize('repair_kind',['column_group','entity_key'])
def test_failed_hierarchy_cannot_erase_rejected_identity_during_repair(decision,repair_kind):
    source,goal,packet=fixture()
    broken=hierarchy(packet);broken['claim']['context_qualifiers']={'tenant':'from_namespace'}
    renamed=composite(packet,repair_kind);renamed['claim']['group_id']='renamed'
    reviewer=Reviewer(decision,repairs=[[renamed]])
    result=DiscoveryResult()
    tools=lambda:SimpleNamespace(context_documents=lambda:[],close=lambda:None)
    with ReviewObservations() as observations:
        rules,*_=_compile_with_repairs(ProposalBatch(task='T0_discovery',proposals=[
            broken,composite(packet),composite(packet,keys=('aux',),extra=True)]),
            source,goal,[],result,model=reviewer,packet=packet,observations=observations,tool_factory=tools)
    assert not rules
    assert reviewer.explorations==1
    assert [p['kind'] for p in reviewer.reviews]==['entity_granularity']
    assert any(i['code']=='semantic_claim_'+decision for i in result.issues)


def test_cached_grain_rejection_is_checked_without_any_hierarchy_context():
    source,goal,packet=fixture();reviewer=Reviewer();result=DiscoveryResult()
    keys=tuple(sorted(_column_id(source,c) for c in ('head','leaf')))
    rules=_compile(ProposalBatch(task='T5_revision',proposals=[composite(packet)]),source,goal,[],result,
        model=reviewer,packet=packet,granularity_rejections={('CatalogObject',keys):'semantic_claim_refuted'})
    assert not rules and not reviewer.reviews
    assert [issue['code'] for issue in result.issues]==['semantic_claim_refuted']


@pytest.mark.parametrize('decision',['supported','refuted'])
def test_new_repair_identity_reviews_previously_uncompiled_hierarchy_as_hypothesis(decision):
    source,goal,packet=fixture()
    broken=hierarchy(packet);broken['claim']['context_qualifiers']={'tenant':'from_namespace'}
    reviewer=Reviewer(decision,repairs=[[composite(packet)]])
    result=DiscoveryResult()
    tools=lambda:SimpleNamespace(context_documents=lambda:[],close=lambda:None)
    with ReviewObservations() as observations:
        rules,*_=_compile_with_repairs(ProposalBatch(task='T0_discovery',proposals=[
            broken,composite(packet,keys=('aux',),extra=True)]),source,goal,[],result,
            model=reviewer,packet=packet,observations=observations,tool_factory=tools)
    assert [p['kind'] for p in reviewer.reviews]==['entity_granularity']
    contexts=reviewer.reviews[0]['hierarchy_identity_context']
    assert contexts and {context['state'] for context in contexts}=={'proposed'}
    assert len(rules)==int(decision=='supported')
    if rules:
        assert rules[0].kind=='column_group_entity'
        assert 'semantic_review:entity_granularity' in rules[0].evidence.semantic_basis
    else:
        assert any(issue['code']=='semantic_claim_refuted' for issue in result.issues)


@pytest.mark.parametrize('kind',['column_group','entity_key'])
def test_late_hierarchy_rechecks_retained_entity_absent_from_final_batch_before_publication(kind):
    source,goal,packet=fixture()
    original=composite(packet,kind)
    original['claim']['group_id']='retained_original'
    reviewer=Reviewer('refuted',repairs=[[hierarchy(packet)]])
    result=DiscoveryResult()
    tools=lambda:SimpleNamespace(context_documents=lambda:[],close=lambda:None)
    with ReviewObservations() as observations:
        rules,*_=_compile_with_repairs(ProposalBatch(task='T0_discovery',proposals=[
            original,composite(packet,keys=('aux',),extra=True)]),source,goal,[],result,
            model=reviewer,packet=packet,observations=observations,tool_factory=tools)
    assert [rule.kind for rule in rules]==['wide_hierarchy']
    reviews=[review for review in reviewer.reviews if review['kind']=='entity_granularity']
    assert len(reviews)==1
    assert reviews[0]['candidate']['claim']['group_id']=='retained_original'
    rows={'public.catalog':[('D','A','B','First','X','X'),('D','A','C','Second','Y','Y')]}
    with MemorySource([source],rows) as adapter,GraphStore() as store:
        assert RuleEngine(store).execute(adapter,rules[0]).published
        graph=store.view({'public'})
        assert len(graph['nodes'])==3 and len(graph['edges'])==2
        assert [rule.kind for rule in store.get_rules()]==['wide_hierarchy']


def test_final_context_check_does_not_repeat_supported_unchanged_grain():
    source,goal,packet=fixture()
    reviewer=Reviewer('supported',repairs=[[composite(packet,keys=('aux',))]])
    result=DiscoveryResult()
    tools=lambda:SimpleNamespace(context_documents=lambda:[],close=lambda:None)
    with ReviewObservations() as observations:
        rules,*_=_compile_with_repairs(ProposalBatch(task='T0_discovery',proposals=[
            composite(packet),hierarchy(packet),composite(packet,keys=('aux',),extra=True)]),
            source,goal,[],result,model=reviewer,packet=packet,observations=observations,tool_factory=tools)
    assert len(rules)==3 and not result.issues
    assert [review['kind'] for review in reviewer.reviews]==['entity_granularity','wide_hierarchy']


def test_new_identity_domain_context_rechecks_previously_supported_grain():
    source,goal,packet=fixture()
    changed=hierarchy(packet);changed['claim']['identity_domain']='another_domain'
    changed['claim']['group_id']='changed_hierarchy'
    class ChangedContextReviewer(Reviewer):
        def complete(self,task,packet,contract):
            if packet['semantic_proposition']['kind']=='entity_granularity':
                self.decision=('refuted' if any(review['kind']=='entity_granularity'
                    for review in self.reviews) else 'supported')
            return super().complete(task,packet,contract)
    reviewer=ChangedContextReviewer(repairs=[[changed]])
    result=DiscoveryResult()
    tools=lambda:SimpleNamespace(context_documents=lambda:[],close=lambda:None)
    with ReviewObservations() as observations:
        rules,*_=_compile_with_repairs(ProposalBatch(task='T0_discovery',proposals=[
            composite(packet),hierarchy(packet),composite(packet,keys=('aux',),extra=True)]),
            source,goal,[],result,model=reviewer,packet=packet,observations=observations,tool_factory=tools)
    assert not [rule for rule in rules if rule.kind=='column_group_entity']
    reviews=[review for review in reviewer.reviews if review['kind']=='entity_granularity']
    assert len(reviews)==2
    assert {'catalog_objects','another_domain'}=={
        context['identity_domain'] for context in reviews[-1]['hierarchy_identity_context']}
    assert any(issue['code']=='semantic_claim_refuted' for issue in result.issues)


def test_fd_repair_cannot_expand_key_into_path_object(monkeypatch):
    source,goal,_=fixture();reviews=[]
    def handle(request):
        _,packet=input_packet(request)
        if packet['task']=='semantic_review':
            proposition=packet['semantic_proposition'];reviews.append(proposition)
            grain=proposition['kind']=='entity_granularity'
            if grain:
                assert any(c['state']=='compiled' for c in proposition['hierarchy_identity_context'])
            return response({'proposition_ref':packet['semantic_proposition_ref'],
                'decision':'refuted' if grain else 'supported','basis':'data_inference','evidence':[{
                    'reference':packet['metadata_digest']['columns'][1]['id'],
                    'stance':'refutes' if grain else 'supports'}]})
        if packet['task']=='T0_discovery':
            return response({'task':packet['task'],'proposals':[composite(packet,keys=('head',)),hierarchy(packet)]})
        assert packet['task']=='T5_revision'
        return response({'task':packet['task'],'proposals':[composite(packet)]})
    rows={'public.catalog':[('D','A','B','First','X','X'),('D','A','C','Second','Y','Y')]}
    with MemorySource([source],rows) as adapter:
        result=discover(adapter,goal=goal,model=client(monkeypatch,handle))
        assert [rule.kind for rule in result.rules]==['wide_hierarchy'],result.issues
        with GraphStore() as store:
            assert RuleEngine(store).execute(adapter,result.rules[0]).published
            graph=store.view({'public'})
            assert len(graph['nodes'])==3 and len(graph['edges'])==2
    assert sum(p['kind']=='entity_granularity' for p in reviews)==1
