"""Goal blockers, optional advice and model failures have separate outcomes."""
import pytest

from lightgraph.construction import runner
from lightgraph.construction.workspace import dump_yaml
from lightgraph.errors import LightGraphError
from lightgraph.web.execution import _safe_report
from lightgraph.tests.test_construction_runner import configuration, scripted_bridge, save_object, completed


@pytest.mark.parametrize('gaps,expected', [([], 'COMPLETED'), (['用户要求的英文别名尚缺映射'], 'COMPLETED_WITH_GAPS')])
def test_explicit_goal_gaps_only_affect_completion(tmp_path, scripted_bridge, gaps, expected):
    config = configuration(tmp_path)
    run_id = 'run_' + 'a' * 24

    def runtime(intent, **kwargs):
        tools = scripted_bridge['tools']
        draft = save_object(tools)
        draft.update(unresolved=gaps, decisions=['相同编码的重复行已去重'], suggestions=['可在上游清理重复行'])
        assert tools.call('save_graph_draft', yaml_text=dump_yaml(draft))['status'] == 'ok'
        validation = tools.call('validate_graph_draft', draft_id=draft['draft_id'])
        assert validation['passed']
        assert validation['statistics']['nodes'] == 2
        assert tools.call('publish_graph_draft', draft_id=draft['draft_id'])['status'] == 'ok'
        return completed(2)

    report = runner.run_construction(config, runtime=runtime, run_id=run_id)
    assert report.status == expected
    assert report.run_id == run_id
    assert report.validation['business_graph'] == 'nonempty'
    assert report.validation['data_checks'] == 'passed'
    assert report.validation['D'] == 'test_double'
    assert report.construction.acceptance['independent_gold'] == 'not_run'
    assert report.construction.summary['published_drafts'] == 1
    assert report.construction.summary['mapping_count'] == 1
    assert report.counts['db_queries'] > 0
    shown = _safe_report(report.model_dump(mode='json'))
    assert shown['construction']['drafts'][0]['suggestions'] == ['可在上游清理重复行']
    assert any(item.get('message') == '可在上游清理重复行' for item in shown['issues'])
    assert any(item.get('message') == '相同编码的重复行已去重' for item in shown['issues'])
    assert [item['message'] for item in shown['issues'] if item['code'] == 'construction_goal_gap'] == gaps


def test_unknown_driver_message_stays_private():
    value = {'run_id': 'run_' + 'a' * 24, 'issues': [
        {'code': 'query_execution_failed', 'message': 'PRIVATE_DRIVER_BODY'},
        {'code': 'construction_suggestion', 'category': 'wrong', 'message': 'PRIVATE_DRIVER_BODY'}]}
    shown = _safe_report(value)
    assert all('message' not in item for item in shown['issues'])


@pytest.mark.parametrize('interrupt', [False, True])
def test_dispatched_calls_survive_missing_budget_or_runtime_result(tmp_path, scripted_bridge, monkeypatch, interrupt):
    config = configuration(tmp_path)

    def native_runtime(intent, **kwargs):
        for payload in (None, [], {'seq': 8, 'type': 'model/request'}):
            kwargs['on_event']({'method': 'model.request', 'payload': payload})
        if interrupt:
            raise LightGraphError('harness_run_interrupted')
        return {'finish_reason': 'completed', 'budget': {}}

    monkeypatch.setattr(runner, 'run_goal', native_runtime)
    report = runner.run_construction(config)
    assert report.counts['llm_calls'] == 8
    assert report.validation['model_usage'] == 'incomplete'
    if interrupt:
        assert report.status == 'PARTIAL'
        assert 'input_tokens' not in report.counts
        assert report.construction.usage == {}
    else:
        assert report.construction.usage['complete'] is False
        assert report.construction.usage['measured_calls'] == 0


@pytest.mark.parametrize('recover,expected', [(False, 'COMPLETED_WITH_GAPS'), (True, 'COMPLETED')])
def test_failed_revalidation_of_published_revision_retains_graph_but_requires_current_success(
        tmp_path, scripted_bridge, monkeypatch, recover, expected):
    config = configuration(tmp_path)
    retained = {}

    def runtime(intent, **kwargs):
        tools = scripted_bridge['tools']
        draft = save_object(tools)
        draft_id = draft['draft_id']
        assert tools.call('validate_graph_draft', draft_id=draft_id)['passed']
        assert tools.call('publish_graph_draft', draft_id=draft_id)['status'] == 'ok'
        retained['graph'] = tools.workspace.read_published_graph()
        retained['publication'] = tools.workspace.read_summary()['publications'][0]

        def failed_projection(*args, **kwargs):
            raise LightGraphError('query_execution_failed')

        # The draft and input file stay unchanged. A real second validation
        # records a local source execution failure on the same published version.
        with monkeypatch.context() as patch:
            patch.setattr(tools.adapters[0], 'project', failed_projection)
            checked = tools.call('validate_graph_draft', draft_id=draft_id)
        assert checked['status'] == 'ok' and not checked['passed']
        assert {item['code'] for item in checked['diagnostics']} == {'query_execution_failed'}
        failed = tools.workspace.get_draft(draft_id)
        assert failed.revision == 1 and failed.status == 'needs_revision'
        assert not failed.validation.passed
        assert tools.workspace.read_published_graph() == retained['graph']
        if recover:
            assert tools.call('validate_graph_draft', draft_id=draft_id)['passed']
        return completed(3)

    report = runner.run_construction(config, runtime=runtime)
    assert report.status == expected
    assert report.rules_published == ['资料对象']
    assert report.counts['business_nodes'] == 2
    assert report.validation['data_checks'] == ('passed' if recover else 'failed')
    assert bool([i for i in report.issues if i['code'] == 'construction_draft_validation_failed']) is not recover
    assert not any(i.get('status') == 'partial' for i in report.issues)
    store = scripted_bridge['tools'].workspace
    assert store.get_draft('资料对象').revision == 1
    assert store.read_published_graph() == retained['graph']
    assert store.read_summary()['publications'][0] == retained['publication']
