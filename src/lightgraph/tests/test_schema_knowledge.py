"""Durable schema hypotheses retain metadata, not source observations or authority."""
import json

import pytest
from pydantic import ValidationError

from lightgraph.contracts import ExtractionGoal, RuleSpec
from lightgraph.discovery.engine import _column_id
from lightgraph.discovery.schema_knowledge import (SchemaKnowledgeLedger, ObjectModelDraft,
    seed_knowledge, relation_candidates, validate_object_model, object_model_reference_slots)
from lightgraph.documents import DocumentCorpus, load_document
from lightgraph.errors import LightGraphError
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import table, client, input_packet, response, group


def setup_ledger(store, tables, goal=None, docs=()):
    corpus=DocumentCorpus(list(docs));corpus.bind_catalog(tables)
    ledger=SchemaKnowledgeLedger(store)
    ledger.prepare(tables,goal or ExtractionGoal(),corpus)
    return ledger


def packet(t):
    return {'task':'T1_table_discovery','metadata_digest':{'columns':[
        {'id':_column_id(t,c.name),'name':c.name} for c in t.columns]},
        'probe_digest':[{'probe_id':'ephemeral_probe'}],
        'document_snippets':[{'doc_id':'definition','revision':'1','section_id':'fields'}]}


def draft(t,**extra):
    return {'object_id':'temporary_model_name','key_columns':[_column_id(t,'item_code')],
        'label_columns':[_column_id(t,'item_cn_name')],**extra}


def rule(t,revision=1):
    return RuleSpec(rule_id='item',revision=revision,kind='column_group_entity',
        source={'source_id':t.source_id,'schema':t.schema_name,'table':t.name},
        identity={'key_columns':['item_code'],'entity_kind':'Metric'},
        attributes={'single_valued':{'label':'item_cn_name'},
            'name_columns':{'english_name':['item_en_name'],'alias':['item_alias']}},
        scope={'namespaces':['tenant_id']})


def test_long_hierarchy_keeps_identity_and_names_in_knowledge():
    t=table('items',['item_code','item_cn_name','item_en_name','item_alias','tenant_id','parent'])
    raw=rule(t).model_dump()
    raw.update(kind='long_hierarchy',hierarchy={'parent_column':'parent','taxonomy':'items',
        'order_evidence':['declared_parent']})
    with GraphStore() as store:
        ledger=setup_ledger(store,[t]);ledger.record_rule(RuleSpec.model_validate(raw))
        remembered=next(o for o in ledger.context(t)['objects'] if o.get('rule_id')=='item')
        assert remembered['key_columns']==[_column_id(t,'item_code')]
        assert remembered['label_columns']==[_column_id(t,'item_cn_name')]


def test_metadata_candidates_include_names_scope_positions_but_never_semantic_proof():
    t=table('items',['tenant_id','item_l1_code','item_l1_cn_name','item_l1_en_name',
        'item_l2_code','item_l2_cn_name','item_l2_en_name','amount','secret_name','password'])
    knowledge=seed_knowledge(t,ExtractionGoal(excluded_columns=['secret_name'],evidence_only_columns=['amount']))
    assert {p['position'] for o in knowledge['objects'] for p in o['positions']}=={1,2}
    assert all(o['status']=='candidate' and o['english_name_columns'] for o in knowledge['objects'])
    assert all(o['namespace_columns']==[_column_id(t,'tenant_id')] for o in knowledge['objects'])
    assert not {'password','secret_name','amount'} & {c['name'] for c in knowledge['columns']}
    assert all('identity_scope' not in o for o in knowledge['objects'])
    # Physical names outside supported hierarchy positions remain ordinary
    # field candidates; arbitrary metadata must not abort ledger preparation.
    assert seed_knowledge(table('other',['item_l99_code','item_l99_cn_name']))['objects'][0]['positions']==[]


def test_candidate_revision_is_retained_without_raw_rows_probe_ids_or_local_identifier(tmp_path):
    path=tmp_path/'ledger.sqlite';t=table('items',['item_code','item_cn_name','item_en_name'])
    with GraphStore(path) as store:
        ledger=setup_ledger(store,[t]);p=packet(t)
        p['strata_samples']=[{'stratum':'s','rows':[{'item_code':'SOURCE_ROW_CANARY'}]}]
        first=draft(t,evidence_refs=['ephemeral_probe','definition@1:fields'])
        ledger.record_models(t,[first],p)
        ledger.record_models(t,[{**first,'english_name_columns':[_column_id(t,'item_en_name')]}],p)
        objects=[o for o in ledger.context(t)['objects'] if o['basis']=='agent_field_model']
        assert len(objects)==1 and objects[0]['english_name_columns']
        assert objects[0]['evidence_refs']==['definition@1:fields']
        stored=''.join(row[0] for row in store.connection.execute('SELECT payload FROM schema_knowledge'))
        assert all(text not in stored for text in ('SOURCE_ROW_CANARY','temporary_model_name','ephemeral_probe'))
        assert ledger.counts()['schema_knowledge_revisions']==3
        version=ledger.context(t)['dependency_version']
    with GraphStore(path) as store:
        ledger=setup_ledger(store,[t])
        assert ledger.context(t)['dependency_version']==version
        assert ledger.context(t)['objects'][0]['basis']=='agent_field_model'
        assert ledger.counts()['schema_knowledge_revisions']==3
        restored=ledger.context(t)['objects'][0]
        ledger.record_models(t,[draft(t,object_id=restored['object_id'])],packet(t))
        assert len([o for o in ledger.context(t)['objects'] if o['basis']=='agent_field_model'])==1


@pytest.mark.parametrize('change',['schema','goal','document'])
def test_dependency_changes_retire_prior_model(change):
    t=table('items',['item_code','item_cn_name','item_en_name'])
    with GraphStore() as store:
        ledger=setup_ledger(store,[t]);ledger.record_models(t,[draft(t)],packet(t))
        old=ledger.context(t)['dependency_version']
        updated=t.model_copy(deep=True);goal=ExtractionGoal();docs=[]
        if change=='schema':updated.columns[0].data_type='integer'
        elif change=='goal':goal.excluded_columns=['item_en_name']
        else:docs=[load_document('fields.md',text='Encoding and naming constraints.')]
        ledger=setup_ledger(store,[updated],goal,docs)
        assert ledger.context(updated)['dependency_version']!=old
        assert not [o for o in ledger.context(updated)['objects'] if o['basis']=='agent_field_model']
        assert {r[0] for r in store.connection.execute(
            'SELECT state FROM schema_knowledge WHERE dependency_version=?',(old,))}=={'stale'}


def test_reprepare_cannot_read_a_removed_source():
    t=table('items',['item_code','item_cn_name'])
    with GraphStore() as store:
        ledger=setup_ledger(store,[t]);ledger.prepare([],ExtractionGoal(),DocumentCorpus([]))
        with pytest.raises(LightGraphError,match='schema_knowledge_source_not_prepared'):ledger.context(t)
        assert relation_candidates([t],ledger)['items']==[]


def test_executed_mappings_preserve_multiple_names_and_publication_conflict_states():
    t=table('items',['item_code','item_cn_name','item_en_name','item_alias','tenant_id'])
    with GraphStore() as store:
        ledger=setup_ledger(store,[t]);ledger.record_rule(rule(t,2),status='published')
        ledger.record_rule(rule(t,1));ledger.record_rule(rule(t,2))
        model=ledger.context(t)['objects'][0]
        assert model['status']=='published' and model['rule_revision']==2
        assert model['english_name_columns']==[_column_id(t,'item_en_name')]
        assert model['alias_columns']==[_column_id(t,'item_alias')]
        ledger.invalidate_rule('item')
        assert next(o for o in ledger.context(t)['objects'] if o.get('rule_id')=='item')['status']=='conflict'


def test_reference_diagnostics_are_field_addressed_and_do_not_echo_rejected_values():
    t=table('items',['item_code','item_cn_name']);p=packet(t)
    invalid=draft(t,key_columns=['PRIVATE_SOURCE_VALUE'])
    with pytest.raises(LightGraphError,match='unauthorized_column_reference'):validate_object_model(invalid,p)
    slots=object_model_reference_slots(ObjectModelDraft.model_validate(invalid),p)
    diagnostic=slots[0].diagnostic(0)
    assert diagnostic['field']==['key_columns',0]
    assert diagnostic['allowed_references']==sorted(_column_id(t,c.name) for c in t.columns)
    assert 'PRIVATE_SOURCE_VALUE' not in json.dumps(diagnostic)
    with pytest.raises(ValidationError):validate_object_model(draft(t,label_columns=[_column_id(t,'item_code')]),p)
    with pytest.raises(ValidationError):validate_object_model(draft(t,notes='PRIVATE_SOURCE_VALUE'),p)


def test_profile_counts_are_revalidation_hints_and_session_edits_have_history():
    t=table('items',['item_code','item_cn_name','item_en_name']);ref=_column_id(t,'item_code')
    with GraphStore() as store:
        ledger=setup_ledger(store,[t]);p=packet(t)
        p['probe_digest']=[{'operator':'profile_columns','probe_id':'probe_0001','aggregates':{
            ref:{'rows':7,'distinct_exact':3,'sample':'SOURCE_CANARY','min_len':1},
            'not_authorized':{'rows':3}}}]
        first=draft(t);second=draft(t,english_name_columns=[_column_id(t,'item_en_name')])
        ledger.record_models(t,[first,second],p)
        assert ledger.counts()['schema_knowledge_revisions']==3
        assert ledger.context(t)['column_counts']=={ref:{'counts':{'rows':7,'distinct_exact':3,'min_len':1},
            'basis':'previous_profile','requires_revalidation':True}}
        stored=''.join(row[0] for row in store.connection.execute('SELECT payload FROM schema_knowledge'))
        assert 'SOURCE_CANARY' not in stored and 'probe_0001' not in stored


def test_relation_index_is_bounded_typed_and_preserves_composite_foreign_keys():
    tables=[table(f'items_{i}',['item_code','item_cn_name','tenant_id']) for i in range(8)]
    from lightgraph.contracts import ForeignKeySpec
    tables[0].foreign_keys=[ForeignKeySpec(name='item_link',columns=['tenant_id','item_code'],
        target_schema='public',target_table='items_1',target_columns=['tenant_id','item_code'])]
    indexed=relation_candidates(tables,max_candidates=3)
    assert len(indexed['items'])==3 and indexed['omitted_occurrences']>0
    first=indexed['items'][0]
    assert first['basis']=='declared_foreign_key' and first['key_arity']==2
    assert first['left']['columns']==first['right']['columns']==['tenant_id','item_code']
    assert all(i['left']['source']!=i['right']['source'] and i['status']=='candidate' for i in indexed['items'])
    assert indexed['value_overlap_checked'] is False
    assert relation_candidates(tables,max_candidates=0)['items']==[]
    assert relation_candidates(tables[:1])['items']==[]
    restricted=relation_candidates(tables,goal=ExtractionGoal(excluded_columns=['tenant_id']))
    assert not any(i['basis']=='declared_foreign_key' for i in restricted['items'])


def test_discovery_receives_seed_and_persists_agent_model_for_next_run(monkeypatch):
    from lightgraph.discovery import discover
    from lightgraph.sources import MemorySource
    from lightgraph.tests.test_proposal_session import tools_response
    t=table('items',['item_code','item_cn_name','item_en_name']);calls=[]
    def handler(request):
        body,p=input_packet(request);calls.append(p)
        assert p['schema_knowledge']['objects'][0]['english_name_columns']
        if len(calls)==1:
            return tools_response(('declare_object_model',draft(t)))
        return response(group(p,'item_code','item_cn_name'))
    with GraphStore() as store,MemorySource([t],{'public.items':[('A','Alpha','Alpha')]}) as source:
        ledger=setup_ledger(store,[t]);model=client(monkeypatch,handler)
        result=discover(source,ExtractionGoal(),model=model,schema_knowledge=ledger)
        assert result.rules, result.issues
        assert any(o['basis']=='agent_field_model' for o in ledger.context(t)['objects'])
        reloaded=setup_ledger(store,[t])
        assert reloaded.context(t)['objects'][0]['label_columns']==[_column_id(t,'item_cn_name')]
