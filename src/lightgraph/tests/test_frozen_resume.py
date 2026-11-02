"""Frozen continuation keeps old output immutable and revalidates copied state."""
from contextlib import closing
import json
import sqlite3

import pytest

from lightgraph.contracts import digest
from lightgraph.errors import LightGraphError
from lightgraph.evaluation.core import fingerprint
from lightgraph.evaluation.frozen import prepare, launch, load, verify, write
from lightgraph.runtime.config import load_config
from lightgraph.tests.test_evaluation import inputs


def parent_run(tmp_path):
    source, gold, config = inputs(tmp_path)
    rules=tmp_path/'rules.json'
    write(rules,[{'rule_id':'items','kind':'column_group_entity',
        'source':{'source_id':'s','table':'source'},
        'identity':{'key_columns':['code'],'entity_kind':'Metric'},
        'attributes':{'single_valued':{'label':'name'}}}])
    body=load(config);body['rules']=[str(rules)];write(config,body)
    parent = tmp_path/'parent'
    prepare(config,parent,gold_path=gold)
    assert launch(parent) in (0,2)
    return source,gold,config,parent


def test_frozen_resume_reuses_state_and_leaves_parent_untouched(tmp_path):
    _,gold,config,parent=parent_run(tmp_path)
    before=fingerprint(parent/'output/graph.sqlite')
    child=tmp_path/'continued'
    manifest=prepare(config,child,gold_path=gold,resume_from=parent)
    assert manifest['resume']['mode']=='resume_and_full_revalidation'
    assert manifest['resume']['candidate_scope_rebound']
    assert not (child/'output').exists()
    assert verify(child)==manifest
    assert launch(child) in (0,2)
    assert load(child/'execution.json')['execution_mode']=='resume_and_full_revalidation'
    report=load(child/'output/report.json')
    assert report['counts']['business_nodes']==1
    assert report['rules_published']==['items']
    assert fingerprint(parent/'output/graph.sqlite')==before
    assert verify(child)==manifest  # Mutable output is separate from immutable seed.


@pytest.mark.parametrize('change',['source','scope','goal'])
def test_resume_rejects_different_inputs_or_authorization(tmp_path,change):
    source,gold,config,parent=parent_run(tmp_path)
    body=load(config)
    if change=='source': source.write_text('code,name\nB,Beta\n')
    elif change=='scope':body['scopes']=['other']
    else:body['goal']={'purpose':'a different extraction goal'}
    write(config,body)
    with pytest.raises(LightGraphError,match='resume_context_changed'):
        prepare(config,tmp_path/'child',gold_path=gold,resume_from=parent)


def test_resume_requires_a_terminal_parent_and_protects_seed(tmp_path):
    _,gold,config=inputs(tmp_path);parent=tmp_path/'parent'
    prepare(config,parent,gold_path=gold)
    with pytest.raises(LightGraphError,match='parent_not_completed'):
        prepare(config,tmp_path/'premature',gold_path=gold,resume_from=parent)
    assert launch(parent) in (0,2)
    child=tmp_path/'child';prepare(config,child,gold_path=gold,resume_from=parent)
    (child/'resume_seed/extra.txt').write_text('untracked state')
    with pytest.raises(LightGraphError,match='frozen_seed_changed'):verify(child)


def test_resume_rebinds_only_the_same_verified_candidate_scope(tmp_path):
    _,gold,config,parent=parent_run(tmp_path)
    old=load_config(parent/'config.json')
    old_key=digest(old.goal.model_dump(mode='json'),sorted(old.scopes),old.isolation_domain,
        [s.model_dump(mode='json',by_alias=True) for s in old.sources])
    # Fixed structural fixture, no samples or source values.
    with closing(sqlite3.connect(parent/'output/graph.sqlite')) as connection, connection:
        connection.execute('INSERT INTO discovery_candidate VALUES(?,?,?,?,?)',
            (old_key,'candidate_fixture',json.dumps({'required_reread':True}),'pending',1))
        connection.execute('INSERT INTO discovery_candidate_event VALUES(?,?,?,?)',
            (old_key,'candidate_fixture',1,'observed'))
    child=tmp_path/'child';prepare(config,child,gold_path=gold,resume_from=parent)
    new=load_config(child/'config.json')
    new_key=digest(new.goal.model_dump(mode='json'),sorted(new.scopes),new.isolation_domain,
        [s.model_dump(mode='json',by_alias=True) for s in new.sources])
    with closing(sqlite3.connect(child/'resume_seed/graph.sqlite')) as connection:
        assert connection.execute('SELECT state FROM discovery_candidate WHERE scope_key=?',(new_key,)).fetchone()==('pending',)
        assert connection.execute('SELECT action FROM discovery_candidate_event WHERE scope_key=?',(new_key,)).fetchone()==('observed',)
        assert not connection.execute('SELECT 1 FROM discovery_candidate WHERE scope_key=?',(old_key,)).fetchone()
