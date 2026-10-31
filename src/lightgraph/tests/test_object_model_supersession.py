"""Explicit replacement retires field hypotheses, never evidence or verified rules."""
from copy import deepcopy
import json

import pytest

from lightgraph.discovery.proposal_session import ProposalSession
from lightgraph.discovery.schema_knowledge import (ObjectModelDraft, SchemaKnowledgeLedger,
    apply_object_model, current_object_knowledge, seed_knowledge, relation_candidates, _mapped_columns)
from lightgraph.errors import LightGraphError
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import table
from lightgraph.tests.test_schema_knowledge import packet, setup_ledger


def fixture():
    t=table('paths',['tenant_id']+[f'item_l{level}_{suffix}' for level in range(1,6)
        for suffix in ('code','cn_name','en_name','alias')])
    knowledge=seed_knowledge(t)
    assert len(knowledge['objects'])==5
    model={'object_id':'path_object','supersedes_object_ids':[o['object_id'] for o in knowledge['objects']]}
    for field in ('key_columns','label_columns','english_name_columns','alias_columns','namespace_columns'):
        model[field]=list(dict.fromkeys(c for o in knowledge['objects'] for c in o[field]))
    model['positions']=[deepcopy(p) for o in knowledge['objects'] for p in o['positions']]
    return t,knowledge,model


def test_five_position_model_retires_only_explicit_seeds_and_preserves_names_history(tmp_path):
    t,_,model=fixture();path=tmp_path/'knowledge.sqlite'
    with GraphStore(path) as store:
        ledger=setup_ledger(store,[t]);original=ledger._load(t);p=packet(t)
        p['schema_knowledge']=ledger.context(t)
        session=ProposalSession(p)
        outcome=session.execute('declare_object_model',model)
        assert outcome['status']=='ok' and outcome['superseded_object_ids']==model['supersedes_object_ids']
        assert len(p['schema_knowledge']['objects'])==1
        assert p['schema_knowledge']['objects'][0]['status']=='candidate'
        ledger.record_models(t,session.batch()._runtime_object_models,p)
        stored=ledger._load(t);visible=ledger.context(t)
        assert len(visible['objects'])==visible['objects_total']==1
        combined=visible['objects'][0]
        assert combined['positions']==model['positions']
        assert combined['english_name_columns']==model['english_name_columns']
        assert combined['alias_columns']==model['alias_columns']
        retired=[o for o in stored['objects'] if o['status']=='superseded']
        assert len(retired)==5 and {o['superseded_by'] for o in retired}=={combined['object_id']}
        for old in original['objects']:
            saved=next(o for o in retired if o['object_id']==old['object_id'])
            assert saved['positions']==old['positions']
        earlier=json.loads(store.connection.execute(
            'SELECT payload FROM schema_knowledge ORDER BY revision LIMIT 1').fetchone()[0])
        assert earlier==original
    with GraphStore(path) as store:
        ledger=setup_ledger(store,[t])
        assert ledger.context(t)['objects']==visible['objects']
        assert len(ledger._load(t)['objects'])==6


@pytest.mark.parametrize('damage',['english','alias','position_alias','position_label','namespace','missing_position'])
def test_incomplete_or_reassociated_replacement_is_atomic_in_session_and_ledger(damage):
    t,knowledge,model=fixture();broken=deepcopy(model)
    if damage=='english':broken['english_name_columns'].pop()
    elif damage=='alias':broken['alias_columns'].pop()
    elif damage=='position_alias':broken['positions'][0]['aliases']=[]
    elif damage=='position_label':
        a,b=broken['positions'][:2];a['label'],b['label']=b['label'],a['label']
    elif damage=='namespace':broken['namespace_columns']=[]
    else:broken['positions'].pop()
    original=deepcopy(knowledge)
    with pytest.raises(LightGraphError,match='object_model_supersession_mapping_loss'):
        apply_object_model(knowledge,broken)
    assert knowledge==original
    p=packet(t);p['schema_knowledge']=knowledge;session=ProposalSession(p)
    assert session.execute('declare_object_model',broken)['code']=='object_model_supersession_mapping_loss'
    assert p['schema_knowledge']==original and not session.batch()._runtime_object_models
    with GraphStore() as store:
        ledger=setup_ledger(store,[t]);before=ledger._load(t)
        with pytest.raises(LightGraphError,match='object_model_supersession_mapping_loss'):
            ledger.record_models(t,[broken],p)
        assert ledger._load(t)==before and ledger.counts()['schema_knowledge_revisions']==1


def test_column_overlap_never_implicitly_retires_an_independent_object():
    t,knowledge,model=fixture();model['supersedes_object_ids']=[]
    result=apply_object_model(knowledge,model)
    assert len(current_object_knowledge(result)['objects'])==6
    assert all(o['status']=='candidate' for o in result['objects'])
    ordinary=table('facts',['metric_code','metric_cn_name','product_code','product_cn_name'])
    knowledge=seed_knowledge(ordinary)
    merged={'object_id':'combined','key_columns':[c for o in knowledge['objects'] for c in o['key_columns']],
        'label_columns':[c for o in knowledge['objects'] for c in o['label_columns']],
        'supersedes_object_ids':[o['object_id'] for o in knowledge['objects']]}
    with pytest.raises(LightGraphError,match='object_model_supersession_mapping_loss'):
        apply_object_model(knowledge,merged)


@pytest.mark.parametrize('damage',['unknown','source','version','validated','published','agent_candidate'])
def test_only_metadata_candidates_owned_by_current_snapshot_can_be_replaced(damage):
    _,knowledge,model=fixture();target=knowledge['objects'][0]
    if damage=='unknown':model['supersedes_object_ids'].append('unissued_source_value')
    elif damage=='source':target['source']={**knowledge['source'],'table':'other'}
    elif damage=='version':target['dependency_version']='stale_version'
    elif damage in {'validated','published'}:
        target.update(basis='executed_rule',status='local_validated' if damage=='validated' else 'published')
    else:target['basis']='agent_field_model'
    original=deepcopy(knowledge)
    with pytest.raises(LightGraphError):apply_object_model(knowledge,model)
    assert knowledge==original


def test_legacy_seed_without_ownership_fields_is_supported_but_same_id_is_not_silently_replaced():
    _,knowledge,model=fixture()
    for obj in knowledge['objects']:
        obj.pop('source');obj.pop('dependency_version')
    assert len(current_object_knowledge(apply_object_model(knowledge,model))['objects'])==1
    model['object_id']=knowledge['objects'][0]['object_id']
    with pytest.raises(LightGraphError,match='object_model_supersession_requires_new_id'):
        apply_object_model(knowledge,model)


def test_revising_combined_model_cannot_erase_superseded_mapping_or_revive_retired_id():
    _,knowledge,model=fixture();result=apply_object_model(knowledge,model)
    view=current_object_knowledge(result)
    revision={**model,'supersedes_object_ids':[]}
    updated=apply_object_model(view,revision)
    assert updated['objects'][0]['supersedes_object_ids']==model['supersedes_object_ids']
    assert len(apply_object_model(view,model)['objects'])==1  # Repeat inherited IDs is idempotent.
    revision['english_name_columns']=[]
    with pytest.raises(LightGraphError,match='object_model_supersession_mapping_loss'):
        apply_object_model(view,revision)
    old=deepcopy(model);old['object_id']=model['supersedes_object_ids'][0]
    with pytest.raises(LightGraphError,match='object_model_already_superseded'):
        apply_object_model(result,old)
    with pytest.raises(LightGraphError,match='object_model_already_superseded'):
        apply_object_model(view,old)


def test_retired_ids_are_rejected_in_same_and_restarted_sessions_before_ledger_replay(tmp_path):
    t,knowledge,model=fixture();path=tmp_path/'retired.sqlite'
    obsolete=deepcopy(model);obsolete.update(object_id=model['supersedes_object_ids'][0],supersedes_object_ids=[])
    with GraphStore(path) as store:
        ledger=setup_ledger(store,[t]);p=packet(t);p['schema_knowledge']=ledger.context(t)
        session=ProposalSession(p)
        assert session.execute('declare_object_model',model)['status']=='ok'
        before=deepcopy(p['schema_knowledge'])
        assert session.execute('declare_object_model',obsolete)['code']=='object_model_already_superseded'
        assert p['schema_knowledge']==before and len(session.object_models)==1
        ledger.record_models(t,session.batch()._runtime_object_models,p)
    with GraphStore(path) as store:
        ledger=setup_ledger(store,[t]);p=packet(t);p['schema_knowledge']=ledger.context(t)
        session=ProposalSession(p)
        assert session.execute('declare_object_model',obsolete)['code']=='object_model_already_superseded'
        assert not session.object_models and len(p['schema_knowledge']['objects'])==1


def test_retired_objects_do_not_count_as_coverage_or_generate_relation_candidates():
    t,knowledge,model=fixture();retired=apply_object_model(knowledge,model)
    objects=[o for o in retired['objects'] if o['status']=='superseded']
    assert not _mapped_columns(objects)
    other=t.model_copy(update={'name':'other'})
    with GraphStore() as store:
        ledger=setup_ledger(store,[t,other])
        assert relation_candidates([t,other],ledger)['items']
        # Exercise index filtering directly: all usable objects were retired,
        # independent of the current prompt's visibility filter.
        value=ledger._load(t)
        for obj in value['objects']:obj.update(status='superseded',superseded_by='object_later')
        from lightgraph.contracts import canonical
        from lightgraph.discovery.schema_knowledge import _source
        with store._transaction():ledger._append(canonical(_source(t)),value)
        assert not relation_candidates([t,other],ledger)['items']
        assert set(ledger._load(t)['unassigned_columns'])=={c['id'] for c in value['columns']}


@pytest.mark.parametrize('status',['local_validated','published'])
def test_an_agent_cannot_reuse_a_verified_object_id_to_overwrite_it(status):
    _,knowledge,model=fixture();target=knowledge['objects'][0]
    target.update(basis='executed_rule',status=status)
    model.update(object_id=target['object_id'],supersedes_object_ids=[])
    with pytest.raises(LightGraphError,match='object_model_verified_revision_required'):
        apply_object_model(knowledge,model)
