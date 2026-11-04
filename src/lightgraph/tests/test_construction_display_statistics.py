"""Display is derived after authorization; topology counts are data, not guesses."""
from copy import deepcopy

import pytest

from lightgraph.construction.access import filter_graph_scopes
from lightgraph.construction.display import derive_display_graph
from lightgraph.construction.executor import execute_draft
from lightgraph.construction.models import GraphDraft
from lightgraph.construction.statistics import directed_topology, graph_statistics
from lightgraph.construction.workspace import GraphWorkspace, safe_load_yaml, write_yaml
from lightgraph.contracts import ColumnSpec, ExtractionGoal, TableSpec, digest
from lightgraph.sources import MemorySource


def node(id, kind, identity, label=None, scope='A', *, display=None):
    support = {'scope_ref': scope, 'label': label, 'aliases': [], 'properties': {},
               'provenance': {'definition': kind, 'mapping': 0, 'source': {'table': 'arbitrary'}}}
    if display:
        support['display_values'] = display
    return {'id': id, 'type': kind, 'identity': identity, 'label': label or 'OLD_BAD_FALLBACK',
            'aliases': [], 'properties': {}, 'provenance': [support['provenance']], 'supports': [support]}


def edge(source, target, kind='关联', scope='A'):
    support = {'scope_ref': scope, 'properties': {}, 'provenance': {
        'definition': kind, 'mapping': 0, 'source': {'table': 'arbitrary'}}}
    return {'id': f'{source}:{kind}:{target}', 'type': kind, 'source': source, 'target': target,
            'properties': {}, 'provenance': [support['provenance']], 'supports': [support]}


def graph(nodes, edges=(), definitions=None):
    return {'nodes': nodes, 'edges': list(edges), 'support_visibility_version': 1,
            'definitions': definitions or {'entities': [], 'relations': []}}


def test_generic_joint_objects_have_readable_unique_labels_without_changing_ids_or_source_names():
    left = node('a', '器材', {'器材编号': '01'}, '相机')
    right = node('b', '场所', {'场所编号': '02'}, '库房')
    first = node('x', '安排', {'日期': '周一', '器材编号': '01', '场所编号': '02'})
    second = node('y', '安排', {'日期': '周二', '器材编号': '01', '场所编号': '02'})
    original = graph([left, right, first, second], [edge(s, 'a', '使用') for s in ['x', 'y']]
                     + [edge(s, 'b', '位于') for s in ['x', 'y']])
    before = deepcopy(original)
    output = derive_display_graph(original)
    by_id = {n['id']: n for n in output['nodes']}
    assert original == before
    assert {n['id'] for n in output['nodes']} == {'a', 'b', 'x', 'y'}
    assert output['edges'] == original['edges']
    assert by_id['a']['label'] == '相机' and by_id['b']['label'] == '库房'
    for key in ['x', 'y']:
        assert '相机' in by_id[key]['display_label'] and '库房' in by_id[key]['display_label']
        assert 'OLD_BAD_FALLBACK' not in by_id[key]['display_label']
        assert by_id[key]['label_origin'] == 'relations'
    assert by_id['x']['label'] != by_id['y']['label']


def test_duplicate_neighbor_names_are_disambiguated_without_relabeling_source_nodes():
    parts = [node('a', '零件', {'零件号': '01'}, '衬套'), node('b', '零件', {'零件号': '02'}, '衬套')]
    contexts = [node('x', '配套', {'零件号': '01'}), node('y', '配套', {'零件号': '02'})]
    output = derive_display_graph(graph(parts + contexts, [edge('x', 'a'), edge('y', 'b')]))
    assert [n['label'] for n in output['nodes'][:2]] == ['衬套', '衬套']
    assert output['nodes'][2]['label'] != output['nodes'][3]['label']
    assert '零件号：01' in output['nodes'][2]['label']


def test_display_uses_only_visible_edges_neighbor_labels_and_declared_supports():
    shared = node('x', '安排', {'编号': 'X'})
    hidden = node('secret', '地点', {'编号': 'S'}, '秘密地点', scope='B')
    visible = node('open', '地点', {'编号': 'O'}, '公开地点')
    original = derive_display_graph(graph([shared, hidden, visible],
        [edge('x', 'secret', scope='B'), edge('x', 'open')]))
    # Old cached relation-derived labels must be discarded before deriving A.
    assert '秘密地点' in original['nodes'][0]['label']
    result = filter_graph_scopes(original, ['A'])
    assert '秘密地点' not in str(result) and 'secret' not in str(result)
    assert '公开地点' in result['nodes'][0]['label']
    shared['supports'].append({**deepcopy(shared['supports'][0]), 'scope_ref': 'B',
                               'display_values': {'说明': '内部说明'}})
    explicit = filter_graph_scopes(graph([shared]), ['A'])
    assert '内部说明' not in str(explicit)
    assert explicit['nodes'][0]['label'] == '安排（编号：X）'


def test_declared_display_is_scoped_and_does_not_replace_source_name():
    first = node('a', '任务', {'编号': '1'}, display={'事项': '检修', '时段': '上午'})
    second = node('b', '任务', {'编号': '2'}, label='原始中文名称', display={'事项': '不应取代'})
    result = derive_display_graph(graph([first, second]))
    assert result['nodes'][0]['label_origin'] == 'declared'
    assert '检修' in result['nodes'][0]['label'] and '上午' in result['nodes'][0]['label']
    assert result['nodes'][1]['label'] == '原始中文名称'
    assert result['nodes'][1]['label_origin'] == 'source'


def test_identity_mode_full_key_and_cycles_do_not_recurse():
    first = node('a', '对象', {'范围': '甲', '码': '001'})
    second = node('b', '对象', {'范围': '乙', '码': '001'})
    original = graph([first, second], [edge('a', 'b'), edge('b', 'a')],
        {'entities': [{'type': '对象', 'display_mode': 'identity'}], 'relations': []})
    result = derive_display_graph(original)
    assert result['nodes'][0]['label'] == '对象（范围：甲；码：001）'
    assert result['nodes'][1]['label'] == '对象（范围：乙；码：001）'
    original['definitions']['entities'][0]['display_mode'] = 'relations'
    assert all(n['label_origin'] == 'relations' for n in derive_display_graph(original)['nodes'])


def test_declared_display_columns_use_complete_authorized_projection():
    table = TableSpec(source_id='s', name='rows', columns=[ColumnSpec(name=c) for c in ['k', 'a', 'b']])
    draft = GraphDraft.model_validate({'draft_id': 'shown', 'entities': [{'type': '调度', 'key_names': ['编号'],
        'mappings': [{'source_id': 's', 'table': 'rows', 'key_columns': ['k'],
                      'display_columns': {'事项': 'a', '地点': 'b'}}]}]})
    with MemorySource([table], {table.qualified_name: [('1', '巡检', '仓库')]}) as adapter:
        result = execute_draft(draft, [adapter], [table])
        assert result['passed'], result['diagnostics']
        item = result['graph']['nodes'][0]
        assert item['identity'] == {'编号': '1'}
        assert item['supports'][0]['display_values'] == {'事项': '巡检', '地点': '仓库'}
        assert '仓库' in item['display_label'] and '巡检' in item['display_label']
        assert item['supports'][0]['label'] is None
        denied = execute_draft(draft, [adapter], [table], goal=ExtractionGoal(excluded_columns=['b']))
        assert not denied['passed']
        assert 'excluded_column_projected' in {d['code'] for d in denied['diagnostics']}
        assert denied['graph']['nodes'] == []


def test_cycle_statistics_find_actual_scc_members_not_downstream_kahn_residue():
    ids = {str(i) for i in range(1500)}
    edges = [edge(str(i), str(i + 1)) for i in range(1499)]
    edges += [edge('2', '1'), edge('900', '900')]
    topology = directed_topology(ids, edges)
    assert topology['acyclic'] is False
    assert topology['cycle_components'] == 2 and topology['cycle_nodes'] == 3
    assert topology['cycle_edges'] == 3
    assert topology['multiple_parent_nodes'] == 2
    assert topology['self_links'] == 1 and topology['roots'] == 1


def test_statistics_count_visible_aliases_mappings_and_mixed_relation_cycles():
    first, second = node('a', '对象', {'id': '1'}, '同名'), node('b', '对象', {'id': '2'}, '同名')
    first['supports'][0]['aliases'] = ['English', 'Alias']
    extra = deepcopy(first['supports'][0])
    extra['provenance']['mapping'] = 1
    first['supports'].append(extra)
    hidden = node('c', '隐藏类型', {'id': '3'}, '隐藏名称', scope='B')
    result = filter_graph_scopes(graph([first, second, hidden], [edge('a', 'b', '甲关系'), edge('b', 'a', '乙关系')]), ['A'])
    stats = graph_statistics(result)
    assert stats['nodes'] == 2 and stats['aliases'] == 2
    assert stats['entity_types']['对象']['observed_mappings'] == 2
    assert stats['entity_types']['对象']['unique_labels'] == 1
    assert stats['entity_types']['对象']['nodes_with_multiple_mappings'] == 1
    assert stats['topology']['cycle_nodes'] == 2
    assert all(value['acyclic'] for value in stats['relation_types'].values())
    assert '隐藏' not in str(stats)


def test_old_saved_draft_fingerprint_survives_new_defaults_and_new_state_records(tmp_path):
    store = GraphWorkspace(tmp_path, context_fingerprint='context')
    old_draft = {'draft_id': 'legacy', 'purpose': '', 'entities': [{'type': '任意', 'description': '',
        'key_names': ['编号'], 'mappings': [{'source_id': 's', 'table': 't', 'schema': 'public',
        'predicate': None, 'properties': {}, 'key_columns': ['code'], 'label_column': None, 'alias_columns': []}]}],
        'relations': [], 'evidence_refs': [], 'rationale': '', 'unresolved': []}
    payload = {'draft': old_draft, 'revision': 1, 'fingerprint': digest(old_draft),
               'context_fingerprint': 'context', 'status': 'candidate', 'validation': None}
    relative = 'drafts/legacy.yaml'
    write_yaml(tmp_path / relative, payload)
    write_yaml(store.manifest_path, {'format_version': 1, 'target': 'graph', 'drafts': {
        'legacy': {'current': relative, 'history': [relative]}}, 'evidence': {}})
    old_bytes = (tmp_path / relative).read_bytes()
    record = store.get_draft('legacy')
    assert record.fingerprint == digest(old_draft)
    assert record.draft.entities[0].display_mode == 'auto'
    assert store.read_summary()['drafts'][0]['mappings'] == 1
    assert store.read_summary()['drafts'][0]['decisions'] == []
    data = graph([node('n', '任意', {'编号': '1'})])
    store.record_validation('legacy', revision=1, passed=True, graph=data)
    assert store.get_draft('legacy').validation.passed
    store.publish('legacy', revision=1, graph=data)
    assert store.get_draft('legacy').status == 'published'
    assert store.read_published_graph()['nodes'][0]['label'] == '任意（编号：1）'
    assert (tmp_path / relative).read_bytes() == old_bytes
    revised = store.save_draft(old_draft)
    assert revised.revision == 2 and revised.status == 'candidate'
    assert revised.fingerprint != payload['fingerprint']
    current = safe_load_yaml(store.manifest_path.read_text())['drafts']['legacy']['current']
    assert 'display_mode' in safe_load_yaml((tmp_path / current).read_text())['draft']['entities'][0]
