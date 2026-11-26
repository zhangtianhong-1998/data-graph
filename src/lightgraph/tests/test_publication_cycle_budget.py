"""An exhausted topology proof cannot certify an acyclic published relation."""
from copy import deepcopy

import pytest

from lightgraph.construction import context_topology, merger
from lightgraph.construction.context_topology import detect_context_cycles
from lightgraph.construction.workflow import publish_premerged_graph
from lightgraph.construction.workspace import GraphWorkspace, read_internal_yaml
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import RunConfig
from lightgraph.tests.test_graph_yaml_workspace import payload


def cycle_inputs(size):
    nodes = [{'id': f'n{i}', 'type': 'Item', 'identity': {'code': str(i)},
              'identity_types': {'code': 'text'}, 'label': str(i), 'properties': {},
              'supports': [{'scope_ref': 'public', 'label': str(i),
                  'business_context': {'status': 'global', 'dimensions': {}},
                  'provenance': {'source': {'source_id': 's', 'schema': 'public', 'table': 'items'},
                                 'key_columns': ['code'], 'label_column': 'code'}}]}
             for i in range(size + 2)]

    def edge(name, source, target, project, kind='parent'):
        return {'id': name, 'type': kind, 'source': f'n{source}', 'target': f'n{target}', 'properties': {},
                'supports': [{'scope_ref': 'public',
                    'business_context': {'status': 'scoped', 'dimensions': {'project': project}},
                    'provenance': {'source': {'source_id': 's', 'schema': 'public', 'table': 'links'},
                                   'source_key_columns': ['a'], 'target_key_columns': ['b']}}]}

    ring = [edge(f'e{i:04}', i, (i + 1) % size, 'M' if i < size // 2 else 'K') for i in range(size)]
    hidden = [edge('z1', size, size + 1, 'M'), edge('z2', size + 1, size, 'M')]
    definitions = {'entities': [{'type': 'Item', 'key_names': ['code']}], 'relations': [
        {'type': kind, 'source_type': 'Item', 'target_type': 'Item', 'acyclic': kind == 'parent',
         'allow_self_links': False} for kind in ('parent', 'ordinary')]}
    parts = [ring[:size // 2], ring[size // 2:], hidden[:1], hidden[1:] + [edge('safe', 0, size, 'M', 'ordinary')]]
    return [{'contributor_id': str(index), 'kind': 'unit', 'graph': {
        'target': 'graph', 'status': 'validated', 'support_visibility_version': 1,
        'definitions': deepcopy(definitions), 'nodes': deepcopy(nodes), 'edges': edges}}
        for index, edges in enumerate(parts)]


def test_default_budget_stops_before_a_real_cycle_and_strict_merge_preserves_old_head(tmp_path):
    units = cycle_inputs(500)
    edges = [edge for unit in units for edge in unit['graph']['edges'] if edge['type'] == 'parent']
    for unit in units:
        result = detect_context_cycles(unit['graph']['edges'])
        assert result['complete'] and not result['cycle_edge_ids']
    result = detect_context_cycles(edges)
    assert not result['complete'] and result['incomplete_reason'] == 'state_limit'
    assert detect_context_cycles(edges, max_states=200_000)['cycle_edge_ids'] == ['z1', 'z2']
    config = RunConfig(sources=[])
    previous = merger.merge_construction_graphs(units[:1])
    publish_premerged_graph(tmp_path, config, merged=previous, included_units=['0'], skipped_units=[], unit_attempts={})
    manifest = tmp_path / 'construction/manifest.yaml'
    before = manifest.read_bytes()
    with pytest.raises(LightGraphError, match='published_relation_constraint_unverified'):
        merger.merge_construction_graphs(units, merge_policy={'shared_entity_types': ['Item'], 'conflict_mode': 'strict'})
    assert manifest.read_bytes() == before


def test_quarantine_omits_unverified_relation_but_keeps_independent_facts(monkeypatch):
    units = cycle_inputs(12)
    monkeypatch.setattr(merger, 'detect_context_cycles', lambda edges: detect_context_cycles(edges, max_states=25))
    result = merger.merge_construction_graphs(units,
        merge_policy={'shared_entity_types': ['Item'], 'conflict_mode': 'quarantine'})
    assert len(result['nodes']) == 14
    assert len(result['edges']) == 1 and result['edges'][0]['type'] == 'ordinary'
    isolation = result['conflict_isolation']
    assert isolation['coverage'] == 'incomplete' and isolation['edges'] == 14
    assert {item['code'] for item in isolation['details']} == {'cross_unit_relation_constraint_unverified'}
    diagnostic, = result['context_cycle_diagnostics']
    assert diagnostic['reason'] == 'context_cycle_analysis_incomplete' and len(diagnostic['edge_ids']) == 14


def test_workspace_inconclusive_constraint_keeps_published_snapshot(tmp_path, monkeypatch):
    units = cycle_inputs(12)
    workspace = GraphWorkspace(tmp_path, 'context')
    for index, unit in enumerate(units):
        name = str(index)
        workspace.save_draft(payload(name, 'Item'))
        workspace.record_validation(name, revision=1, passed=True, graph=unit['graph'])
    workspace.publish('0', revision=1, graph=units[0]['graph'])
    head = read_internal_yaml(workspace.manifest_path)['current_graph']
    previous = workspace.read_published_graph()
    monkeypatch.setattr(context_topology, 'detect_context_cycles',
                        lambda edges: detect_context_cycles(edges, max_states=25))
    with pytest.raises(LightGraphError, match='published_relation_constraint_unverified'):
        workspace.publish('1', revision=1, graph=units[1]['graph'])
    assert read_internal_yaml(workspace.manifest_path)['current_graph'] == head
    assert workspace.read_published_graph() == previous
    assert workspace.get_draft('1').status == 'validated'
