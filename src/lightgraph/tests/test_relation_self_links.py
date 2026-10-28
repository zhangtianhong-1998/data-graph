"""Self-link permission belongs to the business contract, not a predicate name."""
import json

import pytest
from pydantic import ValidationError

from lightgraph.contracts import GraphFact, RuleSpec
from lightgraph.errors import LightGraphError
from lightgraph.rules import RuleEngine
from lightgraph.semantic_schema import SemanticSchema
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore, PersistenceViolation
from lightgraph.store.policy import PersistencePolicy
from lightgraph.tests.test_rules_engine import path, wide_rule, wide_table
from lightgraph.tests.test_rules_engine import completion


OMITTED=object()


def vocabulary(name='related_to',permission=OMITTED):
    relation={'name':name,'description':'A reviewed association between report objects.',
              'source_kinds':['ReportItem'],'target_kinds':['ReportItem']}
    if permission is not OMITTED:relation['allow_self_links']=permission
    return SemanticSchema.model_validate({'relations':[relation]})


def reference(name='related_to',permission=OMITTED, *, reviewed=True):
    return RuleSpec(rule_id='custom_link',kind='reference',source={'source_id':'s','table':'items'},
        identity={'key_columns':['tenant','l2_code','l1_code'],'entity_kind':'ReportItem'},
        relation={'source_rule_id':'tree','target_rule_id':'tree','source_level':2,'target_level':1,
                  'source_columns':['tenant','l2_code'],'target_columns':['tenant','l1_code'],
                  'relation':name},semantic_schema=vocabulary(name,permission),
        evidence={'origin':'discovery','semantic_status':'supported','semantic_decision':'model_review',
                  'conditions_satisfied':True,'semantic_basis':['review:association']+(
                      ['semantic_review:allow_self_links'] if permission is True and reviewed else [])})


@pytest.mark.parametrize('name',['related_to','another_predicate'])
@pytest.mark.parametrize('permission',[OMITTED,False])
def test_forbidden_self_link_rejects_whole_reference_and_preserves_independent_hierarchy(name,permission):
    with MemorySource([wide_table(2)],{'public.items':[path('A','B'),path('A','A')]}) as source,GraphStore() as store:
        engine=RuleEngine(store)
        assert engine.execute(source,wide_rule(2)).published
        before=store.view({'A'})
        rejected=engine.execute(source,reference(name,permission))
        assert not rejected.published and not rejected.prepared
        assert rejected.codes==['reference_self_link_forbidden']
        current=store.view({'A'})
        assert current['nodes']==before['nodes'] and current['edges']==before['edges']
        assert len(current['nodes'])==2 and len(current['edges'])==1
        assert current['edges'][0]['relation']=='parent_of'
        assert all(rule.kind!='reference' for rule in store.get_rules())


def test_explicit_reviewed_reflexive_contract_allows_self_link():
    with MemorySource([wide_table(2)],{'public.items':[path('A','B'),path('A','A')]}) as source,GraphStore() as store:
        engine=RuleEngine(store)
        assert engine.execute(source,wide_rule(2)).published
        outcome=engine.execute(source,reference(permission=True))
        assert outcome.published and not outcome.codes
        links=[edge for edge in store.view({'A'})['edges'] if edge['relation']=='related_to']
        assert len(links)==2 and sum(e['source']==e['target'] for e in links)==1
        assert store.current_versions('custom_link')['reference_self_links']=='explicit_permission_v1'


@pytest.mark.parametrize('change',[
    {'semantic_basis':['review:association']},
    {'semantic_decision':'explicit_declaration'},
    {'semantic_status':'unresolved'},
    {'conditions_satisfied':False},
    {'refuting_refs':['review:counterexample']},
])
def test_self_link_permission_never_substitutes_for_independent_supported_review(change):
    rule=reference(permission=True)
    for key,value in change.items():setattr(rule.evidence,key,value)
    with MemorySource([wide_table(2)],{'public.items':[path('A','A')]}) as source,GraphStore() as store:
        engine=RuleEngine(store)
        assert engine.execute(source,wide_rule(2)).published
        outcome=engine.execute(source,rule)
        assert not outcome.published
        assert set(outcome.codes)&{'reference_self_link_semantics_unreviewed','semantic_evidence_unresolved'}
        assert not store.view({'A'})['edges']


def test_self_link_field_is_boolean_default_and_a_schema_contract_not_silent_override():
    assert vocabulary().relations[0].allow_self_links is False
    assert SemanticSchema.merged(vocabulary(),vocabulary(permission=False)).relations[0].allow_self_links is False
    with pytest.raises(LightGraphError,match='semantic_definition_conflict'):
        SemanticSchema.merged(vocabulary(),vocabulary(permission=True))
    for invalid in ('false','true',0,1,None):
        with pytest.raises(ValidationError):vocabulary(permission=invalid)
    for name in ('parent_of','same_entity'):
        with pytest.raises(PersistenceViolation,match='builtin_relation_definition_forbidden'):
            PersistencePolicy().with_schema(vocabulary(name,True))


def test_store_fact_gate_cannot_be_bypassed_by_renaming_the_predicate():
    for name in ('related_to','another_predicate'):
        policy=PersistencePolicy().with_schema(vocabulary(name))
        with pytest.raises(PersistenceViolation,match='reference_self_link_forbidden'):
            policy.check_fact(GraphFact(fact_kind='Edge',payload={
                'source':'entity_A','target':'entity_A','relation':name}))
    # A metadata reference can describe a physical column mapped to itself.
    PersistencePolicy().check_fact(GraphFact(fact_kind='Edge',payload={
        'source':'column_A','target':'column_A','relation':'verified_reference'}))


def test_publication_rechecks_legacy_staging_without_losing_valid_hierarchy(monkeypatch):
    with MemorySource([wide_table(2)],{'public.items':[path('A','B'),path('A','A')]}) as source,GraphStore() as store:
        engine=RuleEngine(store)
        assert engine.execute(source,wide_rule(2)).published
        with monkeypatch.context() as old_writer:
            old_writer.setattr(PersistencePolicy,'check_relation_self_link',lambda *a,**kw:None)
            staged=engine.execute(source,reference(),defer_publish=True)
        assert staged.prepared
        with pytest.raises(PersistenceViolation,match='reference_self_link_forbidden'):
            store.publish_partitions([completion(pub) for pub in staged.publication_ids])
        assert len(store.view({'A'})['nodes'])==2
        assert [edge['relation'] for edge in store.view({'A'})['edges']]==['parent_of']


def make_legacy_publication(store,source,monkeypatch):
    engine=RuleEngine(store)
    assert engine.execute(source,wide_rule(2)).published
    with monkeypatch.context() as legacy:
        legacy.setattr(PersistencePolicy,'check_relation_self_link',lambda *a,**kw:None)
        result=engine.execute(source,reference())
    assert result.published
    versions=store.current_versions('custom_link');versions.pop('reference_self_links')
    store.connection.execute('UPDATE publication SET source_versions=? WHERE publication_id=?',
                             (json.dumps(versions),result.publication_id))
    # Match old serialized definitions as well as the old validation generation.
    for table_name in ('rule','semantic_catalog'):
        for row in store.connection.execute(f'SELECT rowid AS old_id,payload FROM {table_name}').fetchall():
            value=json.loads(row['payload'])
            schema=value.get('semantic_schema',{}) if table_name=='rule' else value
            for definition in schema.get('relations',[]):definition.pop('allow_self_links',None)
            store.connection.execute(f'UPDATE {table_name} SET payload=? WHERE rowid=?',
                                     (json.dumps(value),row['old_id']))
    return result.publication_id


@pytest.mark.parametrize('self_link',[False,True])
def test_old_custom_publications_require_revalidation_and_keep_history(tmp_path,monkeypatch,self_link):
    database=tmp_path/'graph.sqlite'
    rows=[path('A','B')]+([path('A','A')] if self_link else [])
    with MemorySource([wide_table(2)],{'public.items':rows}) as source,GraphStore(database) as store:
        legacy_id=make_legacy_publication(store,source,monkeypatch)
        original_payload=store.connection.execute('SELECT payload FROM rule WHERE rule_id=?',('custom_link',)).fetchone()[0]
    with MemorySource([wide_table(2)],{'public.items':rows}) as source,GraphStore(database) as store:
        assert len(store.view({'A'})['nodes'])==2
        assert [edge['relation'] for edge in store.view({'A'})['edges']]==['parent_of']
        assert len(store.get_rules())==2  # Keep the executable interpretation for revalidation.
        assert store.connection.execute('SELECT payload FROM rule WHERE rule_id=?',('custom_link',)).fetchone()[0]==original_payload
        assert store.connection.execute('SELECT state FROM publication WHERE publication_id=?',(legacy_id,)).fetchone()[0]=='published'
        assert store.connection.execute('SELECT COUNT(*) FROM publication_support WHERE publication_id=?',(legacy_id,)).fetchone()[0]>0
        engine=RuleEngine(store)
        assert engine.execute(source,wide_rule(2)).published
        result=engine.execute(source,reference())
        assert result.published is not self_link
        if self_link:assert result.codes==['reference_self_link_forbidden']
        else:assert len(store.view({'A'})['edges'])==2
    with GraphStore(database) as store:
        assert len(store.view({'A'})['edges'])==(1 if self_link else 2)
        events=store.connection.execute("SELECT * FROM rule_event WHERE reason_code='reference_self_link_contract_changed'").fetchall()
        assert len(events)==1  # Successful new checks are not retired on every reopen.


def test_unpublished_new_revision_cannot_hide_legacy_current_self_links(tmp_path,monkeypatch):
    database=tmp_path/'graph.sqlite'
    with MemorySource([wide_table(2)],{'public.items':[path('A','B'),path('A','A')]}) as source,GraphStore(database) as store:
        legacy_id=make_legacy_publication(store,source,monkeypatch)
        draft=reference();draft.revision=2;draft.relation.relation='parent_of'
        draft.semantic_schema=SemanticSchema()
        store.put_rule(draft)
    with GraphStore(database) as store:
        assert [edge['relation'] for edge in store.view({'A'})['edges']]==['parent_of']
        head=next(head for head in store.heads() if head['logical_rule_id']=='custom_link')
        assert not head['valid'] and head['publication_id']==legacy_id
        assert len([rule for rule in store.get_rules(include_stale=True) if rule.rule_id=='custom_link'])==2


def test_legacy_builtin_business_reference_also_requires_new_self_link_check(tmp_path,monkeypatch):
    database=tmp_path/'graph.sqlite'
    with MemorySource([wide_table(2)],{'public.items':[path('A','A')]}) as source,GraphStore(database) as store:
        engine=RuleEngine(store);assert engine.execute(source,wide_rule(2)).published
        rule=reference();rule.relation.relation='same_entity';rule.semantic_schema=SemanticSchema()
        with monkeypatch.context() as legacy:
            legacy.setattr(PersistencePolicy,'check_relation_self_link',lambda *a,**kw:None)
            result=engine.execute(source,rule)
        assert result.published
        versions=store.current_versions(rule.rule_id);versions.pop('reference_self_links')
        store.connection.execute('UPDATE publication SET source_versions=? WHERE publication_id=?',
                                 (json.dumps(versions),result.publication_id))
    with GraphStore(database) as store:
        assert len(store.view({'A'})['nodes'])==1 and not store.view({'A'})['edges']
        assert not next(h for h in store.heads() if h['logical_rule_id']=='custom_link')['valid']


def pipeline_model(monkeypatch,packets):
    from lightgraph.tests.test_agent_pipeline import client,input_packet,response
    def handler(request):
        _,packet=input_packet(request);packets.append(packet)
        ids={column['name']:column['id'] for column in packet['metadata_digest']['columns']}
        if packet['task']=='semantic_review':
            return response({'proposition_ref':packet['semantic_proposition_ref'],'decision':'supported',
                'basis':'data_inference','evidence':[{'reference':ids['l1_code'],'stance':'supports'}]})
        if packet['task']=='T0_discovery':
            return response({'task':packet['task'],'proposals':[{'kind':'wide_hierarchy','claim':{
                'group_id':'hierarchy','namespace_columns':[ids['tenant']],
                'levels':[{'position':level,'key':ids[f'l{level}_code'],'label':ids[f'l{level}_name']}
                          for level in (1,2)],'repeat_policy':'collapse_consecutive_equal_identity',
                'ordering_evidence':[ids['l1_code']]},'evidence_cited':[ids['l1_code']]}]})
        if packet['task']=='T2_cross_table' and not packet.get('revision_of'):
            binding=packet['entity_bindings'][0]['rule_id']
            return response({'task':packet['task'],'semantic_schema':vocabulary().model_dump(mode='json'),
                'proposals':[{'kind':'reference','claim':{'source_entity':binding,'target_entity':binding,
                    'source_level':2,'target_level':1,'source_columns':[ids['tenant'],ids['l2_code']],
                    'target_columns':[ids['tenant'],ids['l1_code']],'relation_kind':'related_to'},
                    'evidence_cited':[ids['l2_code']]}]})
        return response({'task':packet['task'],'abstain':True})
    return client(monkeypatch,handler)


def test_pipeline_self_link_counterexample_reaches_fresh_t2_without_source_values(monkeypatch,tmp_path):
    from lightgraph.pipeline import run_pipeline
    from lightgraph.runtime.config import RunConfig
    marker='PRIVATE_SELF_OBJECT_8492';packets=[]
    model=pipeline_model(monkeypatch,packets)
    with MemorySource([wide_table(2)],{'public.items':[path(marker,'Child'),path(marker,marker)]}) as source:
        report=run_pipeline(RunConfig(sources=[],output_dir=str(tmp_path),scopes=['A','public'],
                                     max_identity_reviews=0),[source],model=model)
    revisions=[packet for packet in packets if packet['task']=='T2_cross_table' and packet.get('revision_of')]
    assert len(revisions)==1,report.model_dump()
    packet=revisions[0]
    assert [(item['code'],item['count']) for item in packet['known_conflicts']]==[
        ('reference_self_link_forbidden',1)]
    work=packet['iteration_context']['work_items'][0]
    assert work['pattern_codes']==['reference_self_link_forbidden']
    assert work['observation_counts']=={'distinct_tuples':2,'conflicts':1}
    assert marker not in json.dumps(packet)
    assert report.counts['business_nodes']==2 and report.counts['business_edges']==1
    assert report.status!='COMPLETED'
    assert any(issue['code']=='pattern_review_no_progress' for issue in report.issues)
    with GraphStore(tmp_path/'graph.sqlite') as store:
        graph=store.view({'A','public'})
        assert [edge['relation'] for edge in graph['edges'] if edge['source'] in {
            node['id'] for node in graph['nodes'] if node['layer']=='business'}]==['parent_of']
        unfinished=store.connection.execute("SELECT state,pattern_codes,observation_counts FROM iteration_work WHERE task='review_pattern'").fetchall()
        assert any(row['state']=='unresolved' and 'reference_self_link_forbidden' in row['pattern_codes'] for row in unfinished)
        assert marker not in json.dumps([dict(row) for row in unfinished])


def test_same_goal_pipeline_restores_valid_legacy_reference_without_model_calls(monkeypatch,tmp_path):
    from lightgraph.pipeline import run_pipeline
    from lightgraph.runtime.config import RunConfig
    from lightgraph.tests.test_agent_pipeline import client
    config=RunConfig(sources=[],output_dir=str(tmp_path),scopes=['A','public'],max_identity_reviews=0)
    packets=[]
    with MemorySource([wide_table(2)],{'public.items':[path('A','B')]}) as source:
        first=run_pipeline(config,[source],model=pipeline_model(monkeypatch,packets))
    assert first.counts['business_nodes']==2 and first.counts['business_edges']==2
    database=tmp_path/'graph.sqlite'
    with GraphStore(database) as store:
        rule=next(rule for rule in store.get_rules() if rule.kind=='reference')
        previous=next(head for head in store.heads() if head['logical_rule_id']==rule.rule_id)
        versions=store.current_versions(rule.rule_id);versions.pop('reference_self_links')
        store.connection.execute('UPDATE publication SET source_versions=? WHERE publication_id=?',
                                 (json.dumps(versions),previous['publication_id']))
    with GraphStore(database) as store:
        assert len([edge for edge in store.view({'A','public'})['edges'] if edge['relation']=='related_to'])==0
    def no_call(request):pytest.fail('Cached valid rules must not require model rediscovery.')
    model=client(monkeypatch,no_call)
    with MemorySource([wide_table(2)],{'public.items':[path('A','B')]}) as source:
        second=run_pipeline(config,[source],model=model)
    assert model.usage['calls']==0
    assert second.counts['business_nodes']==2 and second.counts['business_edges']==2,second.model_dump()
    with GraphStore(database) as store:
        current=next(head for head in store.heads() if head['logical_rule_id']==rule.rule_id)
        assert current['valid'] and current['publication_id']!=previous['publication_id']
        assert store.current_versions(rule.rule_id)['reference_self_links']=='explicit_permission_v1'
