from copy import deepcopy

import pytest

from lightgraph.construction.inheritance import load_inherited_source_graph
from lightgraph.construction.merger import load_sharded_graph, merge_construction_graphs, write_sharded_graph
from lightgraph.construction.workspace import safe_load_yaml
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import InheritedGraphRef, RunConfig


def _graph(name):
    return {
        'format_version': 1, 'target': 'graph', 'status': 'published', 'support_visibility_version': 1,
        'definitions': {'entities': [{'type': 'Account', 'key_names': ['code']}], 'relations': []},
        'nodes': [{'type': 'Account', 'id': 'id1', 'identity': {'code': '1'},
                   'supports': [{'scope_ref': 'public', 'label': name}]}],
        'edges': [], 'evidence_refs': [],
    }


def test_inheritance_cannot_widen_source_project_scopes(monkeypatch):
    class Workspace:
        def resolved_config(self, _):
            return RunConfig.model_validate({'sources': [], 'scopes': ['public']})
    ref = InheritedGraphRef(inherit_id='original', source_project_id='project', allowed_scopes=['private'])
    monkeypatch.setattr('lightgraph.web.construction_graph.load_graph', lambda config: _graph('private'))
    with pytest.raises(LightGraphError, match='inherited_graph_scope_not_authorized'):
        load_inherited_source_graph(ref, workspace=Workspace())


def test_unrelated_row_collision_with_inherited_base_keeps_both_identities():
    merged = merge_construction_graphs([
        {'kind': 'base', 'contributor_id': 'ledger', 'graph': _graph('采购应付')},
        {'kind': 'unit', 'contributor_id': 'billing', 'graph': _graph('销售应收')},
    ])
    assert {n['id'] for n in merged['nodes']} >= {'id1'}
    assert len(merged['nodes']) == 2
    assert merged['conflict_isolation']['nodes'] == 0


def test_merge_does_not_drop_incompatible_identity_definitions():
    regional = _graph('采购应付')
    regional['definitions']['entities'][0]['key_names'] = ['region', 'code']
    with pytest.raises(LightGraphError, match='published_definition_conflict'):
        merge_construction_graphs([
            {'kind': 'unit', 'contributor_id': 'ledger', 'graph': _graph('采购应付')},
            {'kind': 'unit', 'contributor_id': 'billing', 'graph': regional},
        ])


def test_published_reader_keeps_old_graph_and_supports_during_next_merge(tmp_path):
    previous = write_sharded_graph(tmp_path, _graph('第一版'), context_fingerprint='old')
    pinned_core = safe_load_yaml((tmp_path / previous['core_path']).read_text('utf-8'))
    replacement = write_sharded_graph(tmp_path, _graph('第二版'), context_fingerprint='new')
    old = load_sharded_graph(tmp_path, pinned_core)
    assert old['nodes'][0]['supports'][0]['label'] == '第一版'
    assert previous['core_path'] != replacement['core_path']
    corrupt = deepcopy(pinned_core)
    corrupt['evidence_shards'] = {}
    with pytest.raises(LightGraphError, match='graph_workspace_record_unavailable'):
        load_sharded_graph(tmp_path, corrupt)

@pytest.mark.parametrize('rows, publish, cycle', [
    ([('1', '2')], True, False), ([], True, False), ([], False, False),
    ([('2', '1')], True, True),
])
def test_base_to_base_delta_report_matches_visible_publication(tmp_path, monkeypatch, rows, publish, cycle):
    from contextlib import contextmanager
    from lightgraph.contracts import ColumnSpec, TableSpec
    from lightgraph.construction import runner
    from lightgraph.construction.models import GraphDraft
    from lightgraph.construction.workspace import dump_yaml, write_yaml
    from lightgraph.sources import MemorySource
    from lightgraph.web.construction_graph import load_graph

    original = _graph('总账')
    original['nodes'].append({'id': 'externally-scoped-id', 'type': 'Account', 'identity': {'code': '2'},
                              'supports': [{'scope_ref': 'public', 'label': '分账'}]})
    source = tmp_path / 'publication.yaml'
    cfg = RunConfig.model_validate({'sources': [], 'model': {'mode': 'in_environment',
                                    'model': 'scripted', 'base_url': 'http://localhost/v1'},
                                    'output_dir': str(tmp_path / 'delta'),
                                    'inherited_graphs': [{'inherit_id': 'ledger', 'snapshot_path': str(source)}]})
    table = TableSpec(source_id='reference', name='links',
                      columns=[ColumnSpec(name='parent_code'), ColumnSpec(name='child_code')])
    draft = {'draft_id': 'link', 'extends_base_graph': True, 'inherited_entity_types': ['Account'],
             'inherited_entity_key_arities': {'Account': 1}, 'entities': [],
             'relations': [{'type': 'contains', 'source_type': 'Account', 'target_type': 'Account', 'acyclic': True,
                            'mappings': [{'source_id': 'reference', 'table': 'links',
                                          'source_key_columns': ['parent_code'], 'target_key_columns': ['child_code']}]}]}
    if cycle:
        original['definitions']['relations'] = [
            GraphDraft.model_validate(draft).relations[0].model_dump(mode='json', exclude={'mappings'})]
        original['edges'] = [{'id': 'base_link', 'type': 'contains',
                              'source': 'id1', 'target': 'externally-scoped-id',
                              'supports': [{'scope_ref': 'public'}]}]
    write_yaml(source, original)
    bound = {}

    @contextmanager
    def serve(tools):
        bound['tools'] = tools
        yield 'http://localhost/mcp', 'test-token'

    def runtime(*args, **kwargs):
        tools = bound['tools']
        if publish:
            assert tools.call('save_graph_draft', yaml_text=dump_yaml(draft))['status'] == 'ok'
            checked = tools.call('validate_graph_draft', draft_id='link')
            assert checked.get('passed'), checked
            assert tools.call('publish_graph_draft', draft_id='link')['status'] == 'ok'
        return {'finish_reason': 'completed', 'budget': {'calls': 1}}

    monkeypatch.setattr(runner, 'serve_local_tools', serve)
    with MemorySource([table], {table.qualified_name: rows}) as adapter:
        report = runner.run_construction(cfg, adapters=[adapter], runtime=runtime)
    visible = load_graph(cfg)
    assert bound['tools'].workspace.read_published_graph()['nodes'] == []
    assert report.status == ('COMPLETED' if publish and not cycle else 'COMPLETED_WITH_GAPS'), report.issues
    assert report.counts['business_nodes'] == len(visible['nodes']) == (2 if publish else 0)
    assert report.counts['business_edges'] == len(visible['edges']) == (0 if cycle else len(rows))
    assert report.counts['delta_nodes'] == 0
    assert report.counts['delta_edges'] == len(rows)
    assert report.validation['published_delta'] == ('nonempty' if rows else 'empty' if publish else 'not_published')
    assert report.validation['business_graph'] == ('nonempty' if publish else 'empty')
    assert report.construction.statistics['integrity']['dangling_edges'] == 0
    if cycle:
        assert report.validation['data_checks'] == 'conflict_isolated'
        assert report.construction.execution['conflict_isolation']['edges'] == 2
        assert report.construction.execution['conflict_isolation']['nodes'] == 0
        assert any(issue['code'] == 'construction_conflicts_isolated' for issue in report.issues)
    if rows and not cycle:
        assert [(edge['source'], edge['target']) for edge in visible['edges']] == [('id1', 'externally-scoped-id')]
        # A later project may inherit the child's published edge, including its original witnesses.
        from lightgraph.construction.inheritance import freeze_inherited_graphs
        child_snapshot = tmp_path / 'child.yaml'
        write_yaml(child_snapshot, visible)
        cfg.inherited_graphs[0].snapshot_path = str(child_snapshot)
        inherited = freeze_inherited_graphs(cfg, tmp_path / 'grandchild')['base_graph']
        assert [(edge['source'], edge['target']) for edge in inherited['edges']] == [('id1', 'externally-scoped-id')]
        assert all(match['inherited_node_id'] for match in inherited['edges'][0]['supports'][0]['lookup_matches'])


def test_explicit_inherited_endpoint_wins_over_unrelated_local_id_collision():
    base = _graph('继承账户')
    local = _graph('本地账户')
    local['extends_base_graph'] = True
    local['nodes'].append({'id': 'order', 'type': 'Order', 'identity': {'id': 'o1'},
                           'supports': [{'scope_ref': 'public', 'label': '订单'}]})
    local['definitions']['entities'].append({'type': 'Order', 'key_names': ['id']})
    local['definitions']['relations'].append({'type': 'uses', 'source_type': 'Order', 'target_type': 'Account'})
    local['edges'] = [{'id': 'link', 'type': 'uses', 'source': 'order', 'target': 'id1',
                       'supports': [{'scope_ref': 'public',
                                     'lookup_matches': [{'endpoint': 'target', 'inherited_node_id': 'id1'}]}]}]
    merged = merge_construction_graphs([
        {'kind': 'base', 'contributor_id': 'ledger', 'graph': base},
        {'kind': 'unit', 'contributor_id': 'orders', 'graph': local},
    ])
    assert len(merged['nodes']) == 3
    assert merged['edges'][0]['target'] == 'id1'
    assert next(n for n in merged['nodes'] if n['id'] == 'id1')['display_label'] == '继承账户'


def test_merge_authority_survives_view_rebuilding():
    from lightgraph.construction.access import filter_graph_scopes
    old = _graph('A_old')
    old['nodes'][0]['supports'][0]['provenance'] = {'source': {'source_id': 'legacy'}}
    approved = _graph('Z_current')
    approved['nodes'][0]['supports'][0]['provenance'] = {'source': {'source_id': 'approved'}}
    merged = merge_construction_graphs([
        {'kind': 'unit', 'contributor_id': 'legacy', 'graph': old},
        {'kind': 'unit', 'contributor_id': 'approved', 'graph': approved},
    ], merge_policy={'shared_entity_types': ['Account'], 'authority_sources': ['approved']})
    visible = filter_graph_scopes(merged, ['public'])
    assert visible['nodes'][0]['display_label'] == 'Z_current'
    assert visible['nodes'][0]['aliases'] == ['A_old']


def test_frozen_snapshot_keeps_old_version_and_checks_config_authorization(tmp_path):
    from lightgraph.construction.inheritance import freeze_inherited_graphs, load_frozen_base_graph
    from lightgraph.construction.workflow import copy_frozen_inheritance
    from lightgraph.construction.workspace import write_yaml

    source = tmp_path / 'publication.yaml'
    write_yaml(source, _graph('最初'))
    cfg = RunConfig.model_validate({'sources': [], 'inherited_graphs': [
        {'inherit_id': 'base', 'snapshot_path': str(source)}]})
    construction = tmp_path / 'construction'
    first = freeze_inherited_graphs(cfg, construction)
    copy_frozen_inheritance(construction, tmp_path / 'unit')
    write_yaml(source, _graph('更新'))
    second = freeze_inherited_graphs(cfg, construction)
    assert first['summary']['inherited'][0]['snapshot_file'] != second['summary']['inherited'][0]['snapshot_file']
    original, _ = load_frozen_base_graph(tmp_path / 'unit', config=cfg)
    assert original['nodes'][0]['display_label'] == '最初'
    changed = cfg.model_copy(deep=True)
    changed.inherited_graphs[0].allowed_scopes = ['private']
    with pytest.raises(LightGraphError, match='inherited_graph_authorization_changed'):
        load_frozen_base_graph(construction, config=changed)
    changed.inherited_graphs = []
    assert load_frozen_base_graph(construction, config=changed)[0] is None
