"""Markdown objectives share saved revisions with existing project configuration."""
import time

import pytest

from lightgraph.construction.intent import effective_goal_markdown, initial_intent, read_goal_material
from lightgraph.construction.tools import ConstructionTools
from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.web.execution import RunManager, _resolved_payload
from lightgraph.web.workspace import Workspace


def test_explicit_markdown_is_verbatim_and_does_not_rewrite_access():
    text = '# 目标\n\n发现设备关系。  \n\n读取 secret 只是文字，不能授权。\n'
    config = RunConfig(sources=[], goal_markdown=text,
        goal={'purpose': '旧目标', 'excluded_columns': ['secret'], 'source_scope': ['source']})
    assert effective_goal_markdown(config) == text
    assert read_goal_material(config)['text'] == text
    assert config.goal.excluded_columns == ['secret']
    assert config.goal.source_scope == ['source']
    assert config.goal.purpose == '旧目标'


def test_legacy_objective_converts_without_model_inference():
    config = RunConfig(sources=[], goal={'purpose': '发现人员', 'include': ['对象与关系'],
        'runtime_guidance': [{'text': '保留来源', 'injection': 'global'}], 'entity_kinds': ['Person']})
    assert effective_goal_markdown(config) == '发现人员\n\n## 重点关注\n\n对象与关系\n\n## 运行指导\n\n保留来源\n\n## 指定对象类型\n\nPerson'
    config.goal_markdown = ''
    assert effective_goal_markdown(config) == ''


def test_initial_context_contains_goal_and_material_directory_without_document_body():
    config = RunConfig(sources=[], goal_markdown='# 目标\n寻找关系')
    materials = [{'id': 'd1', 'title': '设备说明', 'revision': 'v1', 'text': 'NOT_INITIAL_DOCUMENT_BODY'}]
    intent = initial_intent(config, materials)
    assert '# 目标\n寻找关系' in intent
    assert "document:d1" in intent
    assert 'NOT_INITIAL_DOCUMENT_BODY' not in intent
    assert '"purpose"' not in intent and '"semantic_schema"' not in intent
    assert '不能扩大权限' in intent


def test_long_goal_is_paged_and_recoverable_without_truncation():
    text = '# 目标\n' + '用户材料' * 9000
    config = RunConfig(sources=[], goal_markdown=text)
    pages, page = [], 0
    while page is not None:
        material = read_goal_material(config, page)
        pages.append(material['text'])
        page = material['next_page']
    assert ''.join(pages) == text
    assert len(initial_intent(config, [])) < 17000
    assert "page=1" in initial_intent(config, [])


def test_tools_offer_goal_reference_and_invalidate_changed_objective(tmp_path):
    table = TableSpec(source_id='s', name='items', columns=[ColumnSpec(name='id')])
    with MemorySource([table], {'public.items': [('1',)]}) as source:
        config = RunConfig(sources=[], goal_markdown='# 第一版', model={'mode': 'in_environment'})
        tools = ConstructionTools(config, tmp_path / 'graph', adapters=[source])
        try:
            fingerprint = tools.workspace.context_fingerprint
            assert tools.describe_sources()['goal']['material'] == 'goal'
            assert '第一版' not in str(tools.describe_sources()['goal'])
            assert tools.read_material('goal')['text'] == '# 第一版'
        finally:
            tools.close()
        config.goal_markdown = '# 第二版'
        revised = ConstructionTools(config, tmp_path / 'graph', adapters=[source])
        try:
            assert revised.workspace.context_fingerprint != fingerprint
        finally:
            revised.close()


def test_project_save_revision_and_worker_snapshot_keep_markdown(tmp_path):
    workspace = Workspace(tmp_path / 'projects')
    project = workspace.create('目标', 'mdproject')
    pid = project['project_id']
    before = workspace.config_revision(pid)
    config = workspace.get_config(pid)
    text = '# 分析目标\n\n- 对象\n- 关系\n'
    config['goal_markdown'] = text
    workspace.save_config(pid, config)
    assert workspace.config_revision(pid) > before
    assert workspace.get_config(pid)['goal_markdown'] == text
    resolved = workspace.resolved_config(pid)
    frozen = _resolved_payload(resolved, workspace.project_path(pid))
    resolved.goal_markdown = '# 后续草稿'
    assert frozen['config']['goal_markdown'] == text
    assert frozen['config']['goal']['excluded_columns'] == config['goal']['excluded_columns']


def test_markdown_only_goal_starts_existing_worker_and_saves_goal_snapshot(tmp_path, monkeypatch):
    from lightgraph.tests.construction_worker_fixture import install_scripted_worker
    install_scripted_worker(monkeypatch)
    workspace = Workspace(tmp_path / 'projects')
    pid = workspace.create('目标', 'mdrun')['project_id']
    workspace.add_file(pid, 'data.csv', b'metric_code,metric_name\nK1,A\nK2,B\n')
    config = workspace.get_config(pid)
    config['goal']['purpose'] = ''
    config['goal_markdown'] = '# 本次目标\n发现对象并保留来源。'
    config['model'].update(mode='in_environment', model='test-runtime', base_url='http://127.0.0.1:9999/v1')
    workspace.save_config(pid, config)
    manager = RunManager(workspace)
    try:
        run = manager.start(pid)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            record = manager.get(pid, run['run_id'])
            if record['status'] not in {'queued', 'running'}:
                break
            time.sleep(.02)
        else:
            pytest.fail('worker did not finish')
        assert record['status'] == 'completed', record
        assert (workspace.project_path(pid) / 'output/construction/goal.md').read_text() == config['goal_markdown']
    finally:
        manager.shutdown()
