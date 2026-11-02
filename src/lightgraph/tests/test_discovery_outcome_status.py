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
