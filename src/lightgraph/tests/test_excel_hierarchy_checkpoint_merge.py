"""Replay independently published worksheet projections through the default workflow merger."""
from collections import Counter
from copy import deepcopy
from hashlib import sha256

from openpyxl import Workbook

from lightgraph.construction.checkpoints import commit_checkpoint, load_checkpoint
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workflow import effective_merge_policy, load_unit_contributor
from lightgraph.construction.workspace import dump_yaml
from lightgraph.contracts import canonical
from lightgraph.runtime.config import RunConfig


def _draft(number, ref):
    binding = {'source_id': 'excel', 'table': 'tree'}
    kind = f'Member{number}'
    keys = ['parent_code', 'child_code']
    mappings = []
    for key, label, ordinal in zip(keys, ['parent_name', 'child_name'], [2, 5], strict=True):
        mapping = {**binding, 'key_columns': [key], 'label_column': label}
        if number == 4:
            mapping['constant_properties'] = {'position': f'excel_column{ordinal}'}
        mappings.append(mapping)
    reverse = number == 1
    source, target = reversed(keys) if reverse else keys
    context = ['center'] if number < 3 else [*keys, 'leaf_code']
    return {'draft_id': f'projection{number}', 'entities': [
        {'type': kind, 'key_names': ['code'], 'identity_domain': f'domain{number}',
         'identity_scope': 'global', 'identity_evidence_refs': [ref], 'mappings': mappings}],
        'relations': [{'type': f'link{number}', 'source_type': kind, 'target_type': kind,
            'hierarchy_orientation': 'child_to_parent' if reverse else 'parent_to_child',
            'mappings': [{**binding, 'source_key_columns': [source], 'target_key_columns': [target],
                          'context_columns': context}]}]}


def _without_evidence(contributors, missing):
    """Model missing observations in an older checkpoint without rereading the source."""
    def discard_observations(value):
        if isinstance(value, dict):
            for field in missing:
                value.pop(field, None)
            for child in value.values():
                discard_observations(child)
        elif isinstance(value, list):
            for child in value:
                discard_observations(child)
    legacy = deepcopy(contributors)
    discard_observations(legacy)
    return legacy


def _edge_observations(graph):
    nodes = {n['id']: n['identity']['code'] for n in graph['nodes']}
    # An endpoint rewrite can mint a new observation ID. Check the physical
    # evidence and execution history here, and the old endpoint IDs below.
    return Counter((e['type'], nodes[e['source']], nodes[e['target']],
                    canonical({k: v for k, v in s.items() if k not in {'endpoint_origins', 'observation_id'}}))
                   for e in graph['edges'] for s in e['supports'])


def _node_origins(graph):
    return Counter((node['identity']['code'], s['label'], canonical(s['properties']), canonical(origin))
                   for node in graph['nodes'] for s in node['supports'] for origin in s['execution_origins'])


def test_four_units_replay_the_recorded_workbook_without_reopening_it(tmp_path):
    path = tmp_path / 'tree.xlsx'
    book = Workbook()
    sheet = book.active
    sheet.title = 'tree'
    sheet.append(['center', 'parent_code', 'parent_name', 'unused', 'child_code', 'child_name', 'leaf_code'])
    sheet.column_dimensions['D'].hidden = True
    for child, leaf in [('A-01', 'X'), ('A-01', 'Y'), ('A-01', 'Y'), ('B-01', 'Z')]:
        sheet.append(['Operations', 'P', 'Root', 'private', child, 'Supplies', leaf])
    book.save(path)
    book.close()
    snapshot = sha256(path.read_bytes()).hexdigest()
    config = RunConfig(sources=[{'id': 'excel', 'kind': 'excel_workbook', 'path': str(path)}],
                       model={'mode': 'in_environment'})
    checkpoints = []
    for unit, numbers in [('reports', [0, 1]), ('dictionary', [2]), ('ledger', [3]), ('analysis', [4])]:
        output = tmp_path / unit
        tools = ConstructionTools(config, output / 'construction')
        try:
            metadata = tools.call('inspect_source', table='excel:public.tree', operation='get_metadata')
            for n in numbers:
                draft = _draft(n, metadata['evidence_ref'])
                assert tools.call('save_graph_draft', yaml_text=dump_yaml(draft))['status'] == 'ok'
                assert tools.call('validate_graph_draft', draft_id=draft['draft_id'])['passed']
                assert tools.call('publish_graph_draft', draft_id=draft['draft_id'])['status'] == 'ok'
        finally:
            tools.close()
        contributor = load_unit_contributor(output, unit)
        checkpoints.append(commit_checkpoint(tmp_path, 'run_' + 'b' * 24, unit, contributor,
                                             input_digest=snapshot, graph_kind='node_delta'))

    path.unlink()  # Replay has access to committed artifacts only.
    contributors = [load_checkpoint(tmp_path, c) for c in checkpoints]
    policy = effective_merge_policy(config)
    baseline = merge_construction_graphs(contributors, merge_policy=policy, recognize_source_replays=False)
    merged = merge_construction_graphs(contributors, merge_policy=policy)
    assert len(baseline['nodes']) == 15 and len(merged['nodes']) == 3
    assert len(baseline['edges']) == len(merged['edges']) == 10
    for missing, count in [(['source_row_witness'], 6),
                           (['source_row_witness', 'source_column_ordinals'], 9)]:
        legacy = merge_construction_graphs(_without_evidence(contributors, missing), merge_policy=policy)
        assert len(legacy['nodes']) == count and len(legacy['edges']) == 10
    codes = {node['id']: node['identity']['code'] for node in merged['nodes']}
    assert set(codes.values()) == {'P', 'A-01', 'B-01'}
    for edge in merged['edges']:
        source, target = codes[edge['source']], codes[edge['target']]
        parent, child = (target, source) if edge['type'] == 'link1' else (source, target)
        assert parent == 'P' and child in {'A-01', 'B-01'}
        supports = edge['supports']
        assert {s['source_row_witness']['snapshot'] for s in supports} == {snapshot}
        if child == 'A-01':
            spans = [s['source_row_witness']['rows'] for s in supports]
            if edge['type'] in {'link3', 'link4'}:
                assert sorted(spans) == [[[2, 2]], [[3, 4]]]
            else:
                assert spans == [[[2, 4]]]
    assert _edge_observations(merged) == _edge_observations(baseline)
    assert _node_origins(merged) == _node_origins(baseline)
    original = {e['id']: (e['type'], e['source'], e['target']) for e in baseline['edges']}
    accounted = set()
    for edge in merged['edges']:
        if edge['id'] in original:
            accounted.add(edge['id'])
        else:
            for support in edge['supports']:
                origin = support['endpoint_origins'][-1]
                assert original[origin['edge_id']] == (origin['type'], origin['source'], origin['target'])
                accounted.add(origin['edge_id'])
    assert accounted == set(original)
