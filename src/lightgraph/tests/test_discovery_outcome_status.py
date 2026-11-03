"""A finished negative decision differs from an interrupted or broken task."""
import pytest

from lightgraph.errors import LightGraphError
from lightgraph.pipeline import run_pipeline
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import client, group, input_packet, response, table




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
