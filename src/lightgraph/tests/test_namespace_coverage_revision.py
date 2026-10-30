"""Missing source scope must be reviewed and replace, not duplicate, a rule."""
import json

import pytest

from lightgraph.pipeline import run_pipeline
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import client, group, input_packet, response, table
from lightgraph.tests.test_discovery_followup_priority import hierarchy, supported


@pytest.mark.parametrize('kind',['entity','hierarchy'])
def test_scope_coverage_requests_explicit_review_and_replaces_public_rule(monkeypatch,tmp_path,kind):
    cols=['code','name','realm'] if kind=='entity' else ['c1','n1','c2','n2','realm']
    t=table('paths',cols);tasks=[]
    def handler(request):
        _,packet=input_packet(request);tasks.append(packet['task'])
        if packet['task']=='semantic_review':return supported(packet)
        if packet['task']=='T5_revision':
            assert packet['allowed_revision_codes']==['namespace_scope_missing']
            assert packet['revision_candidate']['scope']['namespaces']==[]
            assert packet['iteration_context']['namespace_revision']['proposed_namespace_columns']==['realm']
        assert packet['task'] in ('T0_discovery','T1_table_discovery','T5_revision')
        namespace=None if packet['task']=='T0_discovery' else 'realm'
        if kind=='entity':return response(group(packet,'code','name',namespace=namespace))
        proposal=hierarchy(packet)
        if namespace:
            proposal['claim']['namespace_columns']=[next(c['id'] for c in packet['metadata_digest']['columns'] if c['name']=='realm')]
        return response({'task':packet['task'],'proposals':[proposal]})
    rows=[('A','same','one'),('A','same','two')] if kind=='entity' else [
        ('A','Parent','B','Child','one'),('A','Parent','B','Child','two')]
    model=client(monkeypatch,handler)
    model.config.max_prompt_bytes=131072
    with MemorySource([t],{'public.paths':rows}) as source:
        report=run_pipeline(RunConfig(sources=[],output_dir=str(tmp_path),max_identity_reviews=0),
                            [source],model=model)
    assert tasks.count('T5_revision')==1
    assert tasks[tasks.index('T5_revision')+1]=='semantic_review'
    assert report.counts['coverage_identity_revisions']==1,report.model_dump()
    assert len(report.rules_published)==1
    assert not any(i['code'] in ('unexplained_columns','namespace_scope_missing') for i in report.issues)
    with GraphStore(tmp_path/'graph.sqlite') as store:
        rules=store.get_rules()
        assert len({r.rule_id for r in rules})==1
        latest=max(rules,key=lambda r:r.revision)
        assert latest.scope.namespaces==['realm'] and latest.revision==2
        assert not [n for n in store.view({'public'})['nodes'] if n['layer']=='business']
        nodes=[n for n in store.view({'public','one','two'})['nodes'] if n['layer']=='business']
        assert len(nodes)==(2 if kind=='entity' else 4)


def test_failed_scope_revision_keeps_old_rule_and_actionable_work(monkeypatch,tmp_path):
    t=table('paths',['code','name','realm']);tasks=[]
    def handler(request):
        _,packet=input_packet(request);tasks.append(packet['task'])
        if packet['task']=='T5_revision':return response({'task':packet['task'],'abstain':True})
        assert packet['task'] in ('T0_discovery','T1_table_discovery')
        return response(group(packet,'code','name',namespace='realm' if packet['task']=='T1_table_discovery' else None))
    with MemorySource([t],{'public.paths':[('A','same','one'),('A','same','two')]}) as source:
        report=run_pipeline(RunConfig(sources=[],output_dir=str(tmp_path),max_identity_reviews=0),
                            [source],model=client(monkeypatch,handler))
    assert tasks.count('T5_revision')==1
    assert report.counts['business_nodes']==1 and len(report.rules_published)==1
    assert not report.counts.get('coverage_identity_revisions')
    assert any(i['code']=='namespace_scope_missing' for i in report.issues)
    with GraphStore(tmp_path/'graph.sqlite') as store:
        assert len({r.rule_id for r in store.get_rules()})==1
        row=store.connection.execute("SELECT state,column_refs FROM iteration_work WHERE task='review_pattern' AND pattern_codes LIKE '%namespace_scope_missing%'").fetchone()
        assert row['state'] in ('deferred','unresolved')
        assert json.loads(row['column_refs'])==['realm']
        assert any('namespace_scope_missing' in json.loads(r['codes']) for r in store.connection.execute('SELECT codes FROM discovery_feedback'))


def test_scope_refinement_cannot_remove_scope_change_keys_or_change_predicate():
    from lightgraph.discovery.coverage import namespace_refinement
    from lightgraph.tests.test_rules_engine import wide_rule
    old=wide_rule()
    new=old.model_copy(deep=True)
    new.scope.namespaces=[*old.scope.namespaces,'region']
    new.identity.key_columns=['region',*old.identity.key_columns]
    assert namespace_refinement(old,new) is not None
    assert namespace_refinement(new,old) is None
    changed=new.model_copy(deep=True);changed.identity.key_columns[-1]='different_key'
    assert namespace_refinement(old,changed) is None
    changed=new.model_copy(deep=True);changed.scope.predicate={'region':'one'}
    assert namespace_refinement(old,changed) is None
