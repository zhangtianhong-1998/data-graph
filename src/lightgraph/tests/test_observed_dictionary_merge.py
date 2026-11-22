"""A declared reference and an observed dictionary member have different evidentiary value."""
from copy import deepcopy

import pytest

from lightgraph.construction.access import filter_graph_scopes
from lightgraph.construction.executor import execute_draft
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.contracts import ColumnSpec, ForeignKeySpec, TableSpec
from lightgraph.sources import MemorySource


def _units():
    tables, drafts, values = [], [], {}
    for name in ['registry', 'notes', 'samples']:
        master = name == 'registry'
        keys = ['namespace', 'entry'] if master else ['prefix', 'member']
        table = TableSpec(source_id='archive', name=name,
                          columns=[ColumnSpec(name=c) for c in [*keys, 'caption', 'record_id', 'project']])
        if master:
            table.unique_keys = [keys]
        else:
            table.foreign_keys = [ForeignKeySpec(name='member_reference', columns=keys,
                target_schema='public', target_table='registry', target_columns=['namespace', 'entry'])]
        tables.append(table)
        values[table.qualified_name] = [('N', '02', name, name, 'M')]
        mapping = {'source_id': 'archive', 'table': name, 'applicability': {
            'status': 'scoped', 'columns': {'collection': 'project'}}}
        draft = {'draft_id': name, 'entities': [{'type': 'Member', 'key_names': ['series', 'number'],
                  'mappings': [{**mapping, 'key_columns': keys, 'label_column': 'caption'}]}]}
        if not master:
            draft['entities'].append({'type': 'Record', 'key_names': ['id'], 'mappings': [
                {**mapping, 'key_columns': ['record_id']}]})
            draft['relations'] = [{'type': 'references', 'source_type': 'Record', 'target_type': 'Member',
                                   'mappings': [{**mapping, 'source_key_columns': ['record_id'], 'target_key_columns': keys}]}]
        drafts.append(draft)
    result = []
    with MemorySource(tables, values) as source:
        for draft in drafts:
            checked = execute_draft(draft, [source], tables)
            assert checked['passed'], checked['diagnostics']
            result.append({'contributor_id': draft['draft_id'], 'graph': checked['graph']})
    return result


def _members(graph):
    return [node for node in graph['nodes'] if node['type'] == 'Member']


def test_shared_observed_dictionary_member_rewires_sources_and_preserves_labels_and_ranges():
    units = _units()
    original = deepcopy(units)
    result = merge_construction_graphs(units)
    member, = _members(result)
    assert units == original
    assert result == merge_construction_graphs(units[::-1])
    assert result['fusion_diagnostics']['observed_dictionary_collapsed_nodes'] == 2
    assert result['fusion_diagnostics']['dictionary_eligible_inputs'] == 3
    assert result['fusion_diagnostics']['source_replay_collapsed_nodes'] == 0
    assert set(member['label_variants']) == {'registry', 'notes', 'samples'}
    assert {s['provenance']['source']['table'] for s in member['supports']} == {'registry', 'notes', 'samples'}
    assert len(result['edges']) == 2 and {e['target'] for e in result['edges']} == {member['id']}
    assert filter_graph_scopes(result, ['public'], business_context={'collection': 'K'})['nodes'] == []
    assert len(_members(merge_construction_graphs(units, independent_outputs=True))) == 3
    assert len(_members(merge_construction_graphs(units, recognize_source_replays=False))) == 3


@pytest.mark.parametrize('difference', ['missing_member', 'wrong_member', 'wrong_role', 'other_scope',
                                        'other_project', 'old_snapshot', 'another_dictionary', 'foreign_base'])
def test_missing_or_incompatible_dictionary_witness_leaves_independent_identities(difference):
    units = _units()
    if difference == 'missing_member':
        units = units[1:]
    else:
        master = _members(units[0]['graph'])[0]
        node = _members(units[2]['graph'])[0]
        support = node['supports'][0]
        prov = support['provenance']
        if difference == 'wrong_member':
            master['identity']['number'] = '09'
        elif difference == 'wrong_role':
            prov['catalog_identity_reference']['local_columns'] = prov['key_columns'] = ['other_prefix', 'other_role']
        elif difference == 'other_scope':
            support['scope_ref'] = 'private'
        elif difference == 'other_project':
            support['business_context']['dimensions']['collection'] = 'K'
        elif difference == 'old_snapshot':
            prov.pop('catalog_identity_reference')
        elif difference == 'another_dictionary':
            prov['catalog_identity_reference']['target']['table'] = 'another_registry'
        else:
            units[0]['kind'] = 'base'
    result = merge_construction_graphs(units)
    # Only the compatible fact and its observed registry row can join.
    if difference == 'missing_member':
        assert result['fusion_diagnostics']['dictionary_groups_without_observed_member'] == 1
    blocked_all = difference in {'wrong_role', 'wrong_member', 'foreign_base'}
    assert len(_members(result)) == (3 if blocked_all else 2)
    nodes = {n['id'] for n in result['nodes']}
    assert all(e['source'] in nodes and e['target'] in nodes for e in result['edges'])


def test_a_legacy_replay_group_cannot_be_partially_reassigned_to_a_dictionary():
    units = _units()
    legacy = deepcopy(units[1])
    legacy['contributor_id'] = 'old_notes'
    _members(legacy['graph'])[0]['supports'][0]['provenance'].pop('catalog_identity_reference')
    result = merge_construction_graphs([*units, legacy])
    assert len(_members(result)) == 2
    assert result['fusion_diagnostics']['observed_dictionary_collapsed_nodes'] == 1
    assert any({o['contributor_id'] for s in n['supports'] for o in s.get('execution_origins', [])}
               == {'notes', 'old_notes'} for n in _members(result))


def test_dictionary_added_in_a_later_dag_stage_reconciles_the_original_unit_occurrences():
    master, first, second = _units()
    initial = merge_construction_graphs([first, second])
    assert len(_members(initial)) == 2
    result = merge_construction_graphs([{'kind': 'merged', 'contributor_id': 'stage', 'graph': initial}, master])
    member, = _members(result)
    assert {e['target'] for e in result['edges']} == {member['id']}
    assert result['fusion_diagnostics']['observed_dictionary_collapsed_nodes'] == 2
    replay = merge_construction_graphs([{'kind': 'merged', 'contributor_id': 'complete', 'graph': result}, first])
    assert [n['id'] for n in _members(replay)] == [member['id']]
    assert replay['edges'] == result['edges']


def test_normalizing_an_old_code_does_not_establish_a_match_to_a_different_dictionary_row():
    units = _units()
    node = _members(units[2]['graph'])[0]
    node['supports'][0]['original_key_values'] = {'prefix': 'N', 'member': ' 02'}
    result = merge_construction_graphs(units)
    assert len(_members(result)) == 2


def test_checkpoint_replay_publishes_the_dictionary_membership_and_both_edges(tmp_path):
    from lightgraph.construction.checkpoints import commit_checkpoint, load_checkpoint
    from lightgraph.construction.graph_storage import read_graph_record
    from lightgraph.construction.merger import write_sharded_graph

    inputs = []
    for unit in _units():
        record = commit_checkpoint(tmp_path, 'run_' + 'a' * 24, unit['contributor_id'], unit, input_digest='input')
        inputs.append(load_checkpoint(tmp_path, record))
    merged = merge_construction_graphs(inputs)
    root = tmp_path / 'root'
    head = write_sharded_graph(root, merged, context_fingerprint='ctx')
    visible = filter_graph_scopes(read_graph_record(root / head['core_path']), ['public'])
    member, = _members(visible)
    assert {e['target'] for e in visible['edges']} == {member['id']} and len(visible['edges']) == 2
    assert {s['provenance']['catalog_identity_reference']['kind'] for s in member['supports']} == {
        'unique_key', 'foreign_key'}
