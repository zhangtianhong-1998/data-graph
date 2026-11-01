"""Composite hierarchy-position keys need evidence for their business grain."""
from types import SimpleNamespace

import pytest

from lightgraph.contracts import ExtractionGoal
from lightgraph.discovery import discover
from lightgraph.discovery.engine import _column_id, _source
from lightgraph.discovery.models import DiscoveryResult, ProposalBatch
from lightgraph.discovery.observations import ReviewObservations
from lightgraph.discovery.workflow import _compile, _compile_candidates
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
    def __init__(self,decision='supported'):
        self.decision=decision;self.reviews=[];self.explorations=0
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
        raise AssertionError('Malformed candidates must not start another Agent')


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


def compile_batch(proposals,source,goal,packet,reviewer):
    result=DiscoveryResult()
    with ReviewObservations() as observations:
        rules,*_=_compile_candidates(ProposalBatch(task='T0_discovery',proposals=proposals),
            source,goal,[],result,model=reviewer,packet=packet,observations=observations)
    assert reviewer.explorations==0
    return rules,result


def test_invalid_group_preserves_supported_hierarchy_without_resoliciting_refutation():
    source,goal,packet=fixture();reviewer=Reviewer('refuted')
    rules,result=compile_batch([hierarchy(packet),composite(packet),
        composite(packet,keys=('aux',),extra=True)],source,goal,packet,reviewer)
    assert [rule.kind for rule in rules]==['wide_hierarchy']
    assert sum(review['kind']=='entity_granularity' for review in reviewer.reviews)==1
    assert {issue['code'] for issue in result.issues}=={
        'semantic_claim_refuted','unregistered_attribute_semantics'}


@pytest.mark.parametrize('decision',['refuted','unresolved'])
@pytest.mark.parametrize('kind',['column_group','entity_key'])
def test_malformed_hierarchy_still_supplies_counterevidence_against_path_identity(decision,kind):
    source,goal,packet=fixture();reviewer=Reviewer(decision)
    broken=hierarchy(packet);broken['claim']['context_qualifiers']={'tenant':'from_namespace'}
    rules,result=compile_batch([broken,composite(packet,kind)],source,goal,packet,reviewer)
    assert not rules
    assert [review['kind'] for review in reviewer.reviews]==['entity_granularity']
    assert {context['state'] for context in reviewer.reviews[0]['hierarchy_identity_context']}=={'proposed'}
    assert any(issue['code']=='semantic_claim_'+decision for issue in result.issues)
    assert any(issue['code']=='invalid_hierarchy_context' and issue['status']=='deferred' for issue in result.issues)


def test_cached_grain_rejection_is_checked_without_any_hierarchy_context():
    source,goal,packet=fixture();reviewer=Reviewer();result=DiscoveryResult()
    keys=tuple(sorted(_column_id(source,c) for c in ('head','leaf')))
    rules=_compile(ProposalBatch(task='T5_revision',proposals=[composite(packet)]),source,goal,[],result,
        model=reviewer,packet=packet,granularity_rejections={('CatalogObject',keys):'semantic_claim_refuted'})
    assert not rules and not reviewer.reviews
    assert [issue['code'] for issue in result.issues]==['semantic_claim_refuted']


def test_supported_business_composite_survives_malformed_competing_hierarchy():
    source,goal,packet=fixture();reviewer=Reviewer()
    broken=hierarchy(packet);broken['claim']['context_qualifiers']={'tenant':'from_namespace'}
    rules,result=compile_batch([broken,composite(packet)],source,goal,packet,reviewer)
    assert [rule.kind for rule in rules]==['column_group_entity']
    assert [review['kind'] for review in reviewer.reviews]==['entity_granularity']
    assert 'semantic_review:entity_granularity' in rules[0].evidence.semantic_basis
    assert any(issue['code']=='invalid_hierarchy_context' for issue in result.issues)


def test_independent_bad_candidate_does_not_repeat_supported_grain_review():
    source,goal,packet=fixture();reviewer=Reviewer()
    rules,result=compile_batch([composite(packet),hierarchy(packet),
        composite(packet,keys=('aux',),extra=True)],source,goal,packet,reviewer)
    assert {rule.kind for rule in rules}=={'column_group_entity','wide_hierarchy'}
    assert [review['kind'] for review in reviewer.reviews]==['entity_granularity','wide_hierarchy']
    assert any(issue['code']=='unregistered_attribute_semantics' for issue in result.issues)


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
