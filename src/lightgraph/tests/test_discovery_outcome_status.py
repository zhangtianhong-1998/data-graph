"""A finished negative decision differs from an interrupted or broken task."""
import pytest

from lightgraph.errors import LightGraphError
from lightgraph.pipeline import run_pipeline
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import client, group, input_packet, response, table


@pytest.mark.parametrize('code,status,category,action', [
    ('semantic_claim_refuted','COMPLETED_WITH_GAPS','semantic_decision','continue_other_candidates'),
    ('semantic_recovery_obligation_unresolved','COMPLETED_WITH_GAPS','semantic_decision','continue_other_candidates'),
    ('semantic_recovery_evidence_required','PARTIAL','evidence_gap','gather_evidence'),
    ('recovery_state_invalid','PARTIAL','internal_error','fix_code'),
])
def test_one_table_failure_keeps_healthy_publication_and_distinguishes_decision(
        tmp_path, monkeypatch, code, status, category, action):
    tables=[table(name,['key','label']) for name in ('healthy','affected')]
    attempted=[]
    def handler(request):
        _,packet=input_packet(request)
        if packet['task']=='T2_cross_table':
            return response({'task':packet['task'],'abstain':True})
        return response(group(packet,'key','label'))
    model=client(monkeypatch,handler)
    original=model.explore
    def explore(packet, executor, **kwargs):
        if packet['task']=='T0_discovery':
            attempted.append(packet['scope']['table'])
            if packet['scope']['table']=='affected':raise LightGraphError(code)
        return original(packet,executor,**kwargs)
    model.explore=explore
    rows={'public.healthy':[('A','Alpha')],'public.affected':[('B','Beta')]}
    with MemorySource(tables,rows) as source:
        report=run_pipeline(RunConfig(sources=[],output_dir=str(tmp_path)),[source],model=model)
    assert report.status==status,report.issues
    assert attempted==['healthy','affected']
    issue=next(i for i in report.issues if i['code']==code)
    assert (issue['category'],issue['next_action'])==(category,action)
    assert report.rules_published and report.counts['publication_groups_failed']==0
    with GraphStore(tmp_path/'graph.sqlite') as store:
        business=[n for n in store.view({'public'})['nodes'] if n['layer']=='business']
        assert [n['label'] for n in business]==['Alpha']
    assert bool(report.counts['work_deferred'])==(status=='PARTIAL')


@pytest.mark.parametrize('decision,refs,expected', [
    ('unresolved',[],'semantic_recovery_obligation_unresolved'),
    ('resolved',[],'semantic_revision_evidence_missing'),
    ('resolved',['unissued'],'semantic_review_unissued_evidence'),
])
def test_recovery_verdict_is_not_confused_with_missing_reference(decision,refs,expected):
    from types import SimpleNamespace
    from lightgraph.discovery.semantic_revision import validate_prior_obligations
    verdict=SimpleNamespace(evidence=[],conditions=[],recovery_obligations=[
        SimpleNamespace(candidate_id='old',decision=decision,evidence_refs=refs)])
    guard={'recovery_obligations':[{'candidate_id':'old','required_evidence_refs':['probe_current']}]}
    with pytest.raises(LightGraphError,match=expected):
        validate_prior_obligations(verdict,guard,{'probe_current'})


def test_report_advice_is_derived_from_safe_code_not_arbitrary_input():
    from lightgraph.contracts import RunReport
    from lightgraph.pipeline import _issues
    report=RunReport(run_id='test')
    _issues(report,[{'code':'recovery_state_invalid','status':'partial',
        'category':'SOURCE_SECRET','next_action':'SOURCE_SECRET','message':'SOURCE_SECRET'}])
    assert report.issues==[{'code':'recovery_state_invalid','status':'partial',
        'category':'internal_error','next_action':'fix_code'}]


@pytest.mark.parametrize('history_status',['diagnostic','resolved','rejected'])
def test_recovered_transport_history_does_not_reopen_a_finished_semantic_decision(history_status):
    from types import SimpleNamespace
    from lightgraph.pipeline import _discovery_outcome, _deferred_tables
    issues=[{'code':'model_transport_timeout','status':history_status}]
    assert _discovery_outcome(issues)=='COMPLETED'
    assert not _deferred_tables(SimpleNamespace(issues=issues),['public.t'])
    issues.append({'code':'semantic_claim_refuted','status':'unresolved'})
    assert _discovery_outcome(issues)=='COMPLETED_WITH_GAPS'
    assert not _deferred_tables(SimpleNamespace(issues=issues),['public.t'])
    issues.append({'code':'discovery_submission_pending','status':'deferred'})
    assert _discovery_outcome(issues)=='PARTIAL'
    assert _deferred_tables(SimpleNamespace(issues=issues),['public.t'])=={'public.t'}


def test_unrecovered_failure_still_interrupts_work_even_alongside_diagnostics():
    from lightgraph.pipeline import _discovery_outcome
    assert _discovery_outcome([
        {'code':'semantic_review_protocol_diagnostic','status':'diagnostic'},
        {'code':'model_response_truncated','status':'deferred'},
    ])=='PARTIAL'


def test_relation_investigation_version_changes_with_contract_not_run_id():
    from lightgraph.pipeline import _relation_work_version
    from lightgraph.contracts import RuleSpec
    t=table('items',['code','name'])
    rule=RuleSpec(rule_id='items',kind='column_group_entity',
        source={'source_id':'s','table':'items'},identity={'key_columns':['code'],'entity_kind':'ReportItem'})
    base=_relation_work_version(t,'docs-v1',[rule],[])
    assert base==_relation_work_version(t.model_copy(deep=True),'docs-v1',[rule.model_copy()],[])
    assert base!=_relation_work_version(t,'docs-v2',[rule],[])
    assert base!=_relation_work_version(t,'docs-v1',[rule.model_copy(update={'revision':2})],[])
    changed=t.model_copy(deep=True)
    changed.columns[0].name='new_code'
    assert base!=_relation_work_version(changed,'docs-v1',[rule],[])


def test_finished_relation_gap_does_not_erase_an_operational_failure():
    from lightgraph.contracts import RunReport
    from lightgraph.pipeline import _relation_work_issue, _discovery_outcome
    report=RunReport(run_id='test',issues=[
        {'table':'public.items','code':'relation_discovery_pending','status':'deferred'},
        {'table':'public.other','code':'model_response_truncated','status':'deferred'}])
    _relation_work_issue(report,'public.items','unresolved')
    assert report.issues[-1]['code']=='relation_discovery_unresolved'
    assert _discovery_outcome(report.issues)=='PARTIAL'
    _relation_work_issue(report,'public.items','resolved')
    assert len(report.issues)==1 and report.issues[0]['table']=='public.other'


def test_rejected_endpoint_proposals_close_without_losing_entities_or_repeating(monkeypatch,tmp_path):
    tables=[table('items',['key','label']),table('dimensions',['key','label'])]
    rows={'public.items':[('A','Alpha')],'public.dimensions':[('D','Dimension')]}
    tasks=[]
    retrying=False
    def handler(request):
        _,packet=input_packet(request);tasks.append(packet['task'])
        if packet['task'] in ('T0_discovery','T1_table_discovery'):
            kind='Dimension' if packet['scope']['table']=='dimensions' else 'ReportItem'
            return response(group(packet,'key','label',kind))
        assert packet['task']=='T2_cross_table'
        if retrying:return response({'task':packet['task'],'proposals':[]})
        bindings={b['entity_kind']:b for b in packet['entity_bindings']}
        ids={c['name']:c['id'] for c in packet['metadata_digest']['columns']}
        # verified_reference belongs to physical Column endpoints. Rejecting
        # this business proposal must not leave a task indefinitely pending.
        return response({'task':packet['task'],'proposals':[{'kind':'reference','claim':{
            'source_entity':bindings['ReportItem']['rule_id'],
            'target_entity':bindings['Dimension']['rule_id'],
            'source_columns':[ids['key']],'target_columns':[ids['key']],
            'relation_kind':'verified_reference'},'evidence_cited':[ids['key']]}]})
    config=RunConfig(sources=[],output_dir=str(tmp_path),max_identity_reviews=0)
    for run in range(2):
        tasks.clear()
        with MemorySource(tables,rows) as source:
            report=run_pipeline(config,[source],model=client(monkeypatch,handler))
        assert report.status=='COMPLETED',report.issues
        assert report.counts['business_nodes']==2 and report.counts['business_edges']==0
        assert report.counts['work_deferred']==0 and report.counts['work_unresolved']==0
        if run==0:
            assert tasks.count('T2_cross_table')==2
            assert all(i['status']=='rejected' for i in report.issues
                if i['code']=='relation_endpoint_kind_mismatch')
        else:assert not tasks
    with GraphStore(tmp_path/'graph.sqlite') as store:
        candidates=[dict(row) for row in store.connection.execute('SELECT state FROM discovery_candidate')]
        assert candidates and all(row['state']=='rejected' for row in candidates)
        from lightgraph.runtime.candidates import CandidateQueue
        queue=CandidateQueue(store,config.goal,config.scopes,config.isolation_domain,config.sources)
        candidate=queue.listing()['candidates'][0]
        queue.decide(candidate['candidate_id'],'retry',candidate['revision'])
    retrying=True;tasks.clear()
    with MemorySource(tables,rows) as source:
        retried=run_pipeline(config,[source],model=client(monkeypatch,handler))
    assert retried.status=='COMPLETED',retried.issues
    assert 'T2_cross_table' in tasks
    with GraphStore(tmp_path/'graph.sqlite') as store:
        row=store.connection.execute('SELECT state FROM discovery_candidate WHERE candidate_id=?',
            (candidate['candidate_id'],)).fetchone()
        assert row['state']=='rejected'  # no new result never erases the original rejection
