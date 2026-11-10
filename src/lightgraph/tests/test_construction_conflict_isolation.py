"""Publish complete, clean identities while retaining actionable counterexamples."""
from copy import deepcopy

import pytest

from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import GraphWorkspace, dump_yaml, safe_load_yaml
from lightgraph.construction.reporting import construction_summary, draft_is_ready
from lightgraph.construction.conflicts import isolate_validation, isolate_publications, conflict_details
from lightgraph.contracts import digest
from lightgraph.errors import LightGraphError
from lightgraph.tests.test_construction_tools import config_for, draft
from lightgraph.tests.test_graph_yaml_workspace import payload, graph


def conflict_tools(tmp_path):
    config = config_for(tmp_path)
    (tmp_path / 'input.csv').write_text('编码,名称,归属,密钥\n001,甲,003,x\n002,乙旧名,001,x\n002,乙新名,001,x\n003,丙,001,x\n', encoding='utf-8')
    tools = ConstructionTools(config, tmp_path / 'work')
    table = tools.describe_sources()['tables'][0]
    authored = draft(table)
    authored['relations'] = [{'type': '关联', 'source_type': '设备', 'target_type': '设备',
        'mappings': [{'source_id': table['source'], 'schema': table['schema'], 'table': table['name'],
                      'source_key_columns': ['编码'], 'target_key_columns': ['归属']}]}]
    assert tools.call('save_graph_draft', yaml_text=dump_yaml(authored))['status'] == 'ok'
    return config, tools, authored


def test_local_conflict_publishes_clean_remainder_without_losing_counterexamples(tmp_path):
    config, tools, authored = conflict_tools(tmp_path)
    try:
        result = tools.call('validate_graph_draft', draft_id='设备')
        assert result['passed'] is False
        assert result['publishable_subset'] == {'nodes': 2, 'edges': 2, 'isolated_nodes': 1, 'isolated_edges': 1}
        record = tools.workspace.get_draft('设备')
        candidate = tools.workspace.read_validation_graph('设备', record.validation)
        before = digest(candidate)
        published = tools.call('publish_graph_draft', draft_id='设备')
        assert published['status'] == 'ok', published
        assert (published['nodes'], published['edges']) == (2, 2)
        current = tools.workspace.read_published_graph()
        assert {n['identity']['编码'] for n in current['nodes']} == {'001', '003'}
        assert all(e['source'] != e['target'] for e in current['edges'])
        assert before == digest(tools.workspace.read_validation_graph('设备', record.validation))
        assert tools.workspace.get_draft('设备').validation.passed is False
        pubs = {p['draft_id']: p for p in tools.workspace.read_summary()['publications']}
        assert not draft_is_ready(tools.workspace.get_draft('设备'), pubs)
        report = construction_summary(tools.workspace.list_drafts(), pubs, current)
        assert report.execution['conflict_isolation']['nodes'] == 1
        assert report.execution['instance_publications'][0]['coverage'] == 'conflict_isolated'
        assert tools.call('publish_graph_draft', draft_id='设备')['status'] == 'ok'

        # A new draft revision and a disconnected database cannot erase the old
        # names/keys. Reading that evidence does not authorize the new draft.
        authored['purpose'] = '待核查旧名和新名'
        tools.call('save_graph_draft', yaml_text=dump_yaml(authored))
        def no_query(*args, **kwargs):
            raise AssertionError('must not read facts for retained diagnostics')
        for adapter in tools.adapters:
            adapter.execute = no_query
        evidence = tools.call('read_material', name='conflicts:设备')
        assert evidence['status'] == 'ok', evidence
        details = safe_load_yaml(evidence['content'])
        assert details['observed_revision'] == 1 and details['current_revision'] == 2
        assert details['requires_database'] is False
        assert details['nodes'][0]['identity'] == {'编码': '002'}
        assert {n['label'] for n in details['nodes'][0]['names']} == {'乙旧名', '乙新名'}
        assert all(n['sources'][0]['label_column'] == '名称' for n in details['nodes'][0]['names'])
        assert tools.call('publish_graph_draft', draft_id='设备')['code'] == 'graph_validation_required'
    finally:
        tools.close()


@pytest.mark.parametrize('failure', ['memory_budget_exhausted', 'returned_bytes_budget_exhausted',
    'returned_rows_budget_exhausted', 'projection_truncated', 'database_reconnect_failed', 'relation_cycle'])
def test_other_failures_cannot_authorize_subset(tmp_path, failure):
    work = GraphWorkspace(tmp_path, 'context')
    work.save_draft(payload('objects'))
    candidate = graph()
    work.record_validation('objects', revision=1, passed=False, graph=candidate,
        diagnostics=[{'code': 'same_identity_conflicting_label', 'node_id': candidate['nodes'][0]['id']}, {'code': failure}])
    with pytest.raises(LightGraphError, match='graph_validation_required'):
        work.publish('objects', revision=1, graph=candidate, isolate_conflicts=True)
    assert work.read_published_graph()['nodes'] == []


def test_tampered_candidate_cannot_publish_subset(tmp_path):
    _, tools, _ = conflict_tools(tmp_path)
    try:
        tools.call('validate_graph_draft', draft_id='设备')
        record = tools.workspace.get_draft('设备')
        candidate = tools.workspace.read_validation_graph('设备', record.validation)
        candidate['nodes'][0]['label'] = 'forged'
        with pytest.raises(LightGraphError, match='validated_graph_mismatch'):
            tools.workspace.publish('设备', revision=1, graph=candidate, isolate_conflicts=True)
    finally:
        tools.close()


def test_cross_draft_conflict_quarantines_both_contributors_and_can_be_repaired(tmp_path):
    work = GraphWorkspace(tmp_path, 'context')
    def publish(name, candidate, *, revision=1):
        work.save_draft(payload(name))
        work.record_validation(name, revision=revision, passed=True, graph=candidate)
        return work.publish(name, revision=revision, graph=candidate, isolate_conflicts=True)
    first = graph(label='Old name')
    clean = deepcopy(first['nodes'][0]); clean['id'] = 'clean'; clean['identity'] = {'code': 'clean'}
    first['nodes'].append(clean)
    entry = publish('first', first)
    original = (work.root / entry['graph']).read_bytes()
    second = graph(label='New name')
    publish('second', second)
    merged = work.read_published_graph()
    assert [n['id'] for n in merged['nodes']] == ['clean']
    assert merged['conflict_isolation']['nodes'] == 1
    assert (work.root / entry['graph']).read_bytes() == original
    # Revalidate the repaired contributor. Quarantine is recomputed from the
    # active publication set, not a permanent blacklist.
    publish('second', graph(label='Old name'))
    assert len(work.read_published_graph()['nodes']) == 2
    assert work.read_published_graph()['conflict_isolation']['nodes'] == 0


def test_scope_filtered_counterexamples_do_not_reveal_hidden_names(tmp_path):
    _, tools, _ = conflict_tools(tmp_path)
    try:
        tools.call('validate_graph_draft', draft_id='设备')
        record = tools.workspace.get_draft('设备')
        candidate = tools.workspace.read_validation_graph('设备', record.validation)
        assert conflict_details(candidate, record.validation.diagnostics, [])['nodes'] == []
        from lightgraph.construction.access import filter_graph_scopes
        isolated = isolate_validation(candidate, record.validation.diagnostics)
        assert 'node_ids' not in filter_graph_scopes(isolated, ['public'])['conflict_isolation']
    finally:
        tools.close()


from lightgraph.tests.test_construction_runner import configuration, scripted_bridge, save_object, completed
from lightgraph.construction.runner import run_construction


def test_runner_reports_usable_subset_and_goal_gap(tmp_path, scripted_bridge):
    config = configuration(tmp_path)
    (tmp_path / 'items.csv').write_text('代号,中文名\n001,甲\n002,旧乙\n002,新乙\n', encoding='utf-8')
    def runtime(*args, **kwargs):
        tools = scripted_bridge['tools']
        save_object(tools)
        checked = tools.call('validate_graph_draft', draft_id='资料对象')
        assert checked['publishable_subset']['nodes'] == 1
        assert tools.call('publish_graph_draft', draft_id='资料对象')['status'] == 'ok'
        return completed()
    report = run_construction(config, runtime=runtime)
    assert report.status == 'COMPLETED_WITH_GAPS'
    assert report.counts['business_nodes'] == 1 and report.counts['published_drafts'] == 1
    assert 'construction_conflicts_isolated' in {i['code'] for i in report.issues}
    assert report.construction.execution['conflict_isolation']['nodes'] == 1
    assert report.validation['data_checks'] == 'conflict_isolated'


def test_runner_cross_draft_conflicts_are_gaps_even_when_both_local_checks_pass(tmp_path, scripted_bridge):
    config = configuration(tmp_path)
    (tmp_path / 'items.csv').write_text('代号,中文名,别名\n001,甲,甲\n002,旧乙,新乙\n', encoding='utf-8')
    def runtime(*args, **kwargs):
        tools = scripted_bridge['tools']
        authored = save_object(tools)
        assert tools.call('validate_graph_draft', draft_id=authored['draft_id'])['passed']
        assert tools.call('publish_graph_draft', draft_id=authored['draft_id'])['status'] == 'ok'
        authored['draft_id'] = 'other'
        authored['entities'][0]['mappings'][0]['label_column'] = '别名'
        tools.call('save_graph_draft', yaml_text=dump_yaml(authored))
        assert tools.call('validate_graph_draft', draft_id='other')['passed']
        assert tools.call('publish_graph_draft', draft_id='other')['status'] == 'ok'
        result = tools.call('read_material', name='publication_conflicts')
        assert result['status'] == 'ok', result
        evidence = safe_load_yaml(result['content'])
        assert len(evidence['contributors']) == 2
        labels = {n['label'] for c in evidence['contributors'] for node in c['nodes'] for n in node['names']}
        assert labels == {'旧乙', '新乙'}
        return completed()
    report = run_construction(config, runtime=runtime)
    assert report.status == 'COMPLETED_WITH_GAPS'
    assert report.counts['business_nodes'] == 1 and report.counts['published_drafts'] == 2
    assert all(d.validation.passed for d in report.construction.drafts)
    assert report.construction.execution['conflict_isolation']['nodes'] == 1
    assert report.validation['data_checks'] == 'conflict_isolated'
