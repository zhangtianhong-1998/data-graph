"""Historical projections preserve their source observations when member identities coalesce."""
from collections import Counter
from copy import deepcopy

import pytest

from lightgraph.construction.executor import execute_draft
from lightgraph.construction.fusion import relation_identity
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.contracts import ColumnSpec, TableSpec, canonical
from lightgraph.sources import MemorySource
from lightgraph.tests.test_source_hierarchy_replays import code, contributor, draft_graph


KEYS = ('UNIT_L1_CODE', 'UNIT_L2_CODE')
NAMES = ('UNIT_L1_NAME', 'UNIT_L2_NAME')
ROWS = [('CAT', 'Catalogue', 'S01', 'Supplies', 'A'),
        ('CAT', 'Catalogue', 'B01', 'Supplies', 'A'),
        ('ARC', 'Archives', 'S02', 'Records', 'A')]


def _projection(index, *, conflict=False, business_filter=False, other_context=False,
                constants=None, repeated_position=False, disputed_position=False):
    table = TableSpec(source_id='file', name='vocabulary', columns=[
        ColumnSpec(name=c) for c in ('collection', KEYS[0], NAMES[0], KEYS[1], NAMES[1])])
    rows = ROWS.copy()
    if conflict:
        # Even the inconsistent member's different recorded names must survive.
        rows.append(('CAT', 'Catalogue', 'S01', 'Supply items', 'A'))
    if repeated_position:
        rows.append(('S02', 'Records', 'S02', 'Records', 'A'))
    kinds = ['Record', 'Record', 'IndexEntry', 'Member', 'Topic']
    kind = kinds[index]
    inverse = index == 1
    logical_key = 'accession' if index % 2 else 'identifier'
    node_mappings = []
    for position, (key, label) in enumerate(zip(KEYS, NAMES), start=1):
        context = [list(KEYS), [KEYS[1]], [], [KEYS[0]], list(reversed(KEYS))][index]
        if other_context:
            context = [*context, 'collection']
        location = [
            {'tree_level': f'L{position}', 'position': f'UNIT_L{position}'},
            {},
            {'tree_level': position, 'position': f'vocabulary_level{position}'},
            {'tree_level': f'L{position}'},
            {},
        ][index]
        if disputed_position:
            location['position'] = f'excel_column_{position * 2}'
        m = {'source_id': 'file', 'table': 'vocabulary', 'key_columns': [key],
             'label_column': label, 'context_columns': context,
             'constant_properties': {**location, **(constants or {})}}
        guard = {'column': key, 'op': 'is_not_null'}
        if index == 0:
            m['predicate'] = guard
        elif index == 2:
            m['predicate'] = {'all': [guard, {'all': [deepcopy(guard)]}]}
        if business_filter:
            m['predicate'] = {'all': [guard, {'column': 'collection', 'op': 'eq', 'value': 'A'}]}
        node_mappings.append(m)
    src, dst = reversed(KEYS) if inverse else KEYS
    edge_mapping = {'source_id': 'file', 'table': 'vocabulary', 'source_key_columns': [src],
                    'target_key_columns': [dst], 'equal_endpoints': 'omit',
                    'context_columns': list(node_mappings[0]['context_columns'])}
    if index == 2:
        edge_mapping['predicate'] = {'all': [{'column': src, 'op': 'is_not_null'},
                                            {'column': dst, 'op': 'is_not_null'}]}
    draft = {
        'draft_id': f'map{index}',
        'entities': [{'type': kind, 'key_names': [logical_key], 'identity_domain': f'domain{index}',
                      'identity_scope': 'global', 'identity_evidence_refs': [f'e{index}'], 'mappings': node_mappings}],
        'relations': [{'type': f'connection{index}', 'source_type': kind, 'target_type': kind,
                       'hierarchy_orientation': 'child_to_parent' if inverse else 'parent_to_child',
                       'mappings': [edge_mapping]}],
    }
    values = [(project, k1, n1, k2, n2) for k1, n1, k2, n2, project in rows]
    with MemorySource([table], {table.qualified_name: values}) as adapter:
        result = execute_draft(draft, [adapter], [table], label_conflict_policy='retain_variants')
    assert result['passed'], result['diagnostics']
    graph = result['graph']
    graph.update(draft_id=draft['draft_id'], draft_revision=1)
    return graph


def _facts(graph):
    """Compare actual physical witnesses across canonical ID and relation-type rewrites."""
    nodes = {n['id']: n for n in graph['nodes']}
    relations = {r['type']: r for r in graph['definitions']['relations']}
    witnesses = Counter()
    for item in [*graph['nodes'], *graph['edges']]:
        if 'source' in item:
            r = relations[item['type']]
            assert r['source_type'] == nodes[item['source']]['type']
            assert r['target_type'] == nodes[item['target']]['type']
            semantic_type = r.get('original_type') or item['type']
            binding = (semantic_type, r.get('hierarchy_orientation'), code(nodes[item['source']]),
                       code(nodes[item['target']]))
        else:
            binding = ('node', code(item))
        for s in item['supports']:
            facts = {k: s[k] for k in ('scope_ref', 'required_scope_refs', 'business_context', 'row_context',
                                       'label', 'properties', 'provenance') if k in s}
            origins = s.get('execution_origins') or [s]
            for o in origins:
                witnesses[binding, canonical(facts), o.get('contributor_id', s.get('contributor_id'))] += 1
    return witnesses


def test_five_drafts_keep_source_paths_when_only_projection_evidence_differs():
    units = [contributor('u1', _projection(0, repeated_position=True), _projection(1, repeated_position=True)),
             *(contributor(f'u{i}', _projection(i, repeated_position=True)) for i in range(2, 5))]
    before = merge_construction_graphs(units, recognize_source_replays=False)
    after = merge_construction_graphs(units)
    assert Counter(map(code, before['nodes'])) == {'CAT': 5, 'ARC': 5, 'S01': 5, 'B01': 5, 'S02': 5}
    assert Counter(map(code, after['nodes'])) == {'CAT': 1, 'ARC': 1, 'S01': 1, 'B01': 1, 'S02': 1}
    assert len(before['edges']) == len(after['edges']) == 15
    assert after['fusion_diagnostics']['hierarchy_replay_diagnostics']['physical_set_outcomes'] == [
        {'input_nodes': 5, 'output_nodes': 1, 'sets': 5}]
    assert _facts(before) == _facts(after)


def test_multiple_names_on_one_member_only_block_its_own_identity():
    units = [contributor(f'u{i}', _projection(i, conflict=True)) for i in range(3)]
    before = merge_construction_graphs(units, recognize_source_replays=False)
    after = merge_construction_graphs(units)
    assert Counter(map(code, after['nodes'])) == {'CAT': 1, 'ARC': 1, 'S01': 3, 'B01': 1, 'S02': 1}
    assert [n['id'] for n in after['nodes'] if code(n) == 'S01'] == [
        n['id'] for n in before['nodes'] if code(n) == 'S01']
    assert len(before['edges']) == len(after['edges']) == 9
    assert _facts(before) == _facts(after)
    # The source has legitimate competing definitions for Record. Specializing
    # an edge's endpoint type must not introduce an unresolved relation family.
    old_conflicts = {(c['category'], c['original_type']) for c in before['definition_conflicts']}
    assert {(c['category'], c['original_type']) for c in after['definition_conflicts']} == old_conflicts
    # The next workflow node reads the published merge as a frozen input.
    republished = merge_construction_graphs([{'kind': 'merged', 'contributor_id': 'tree', 'graph': after}])
    assert {e['id'] for e in republished['edges']} == {e['id'] for e in after['edges']}
    assert _facts(republished) == _facts(after)
    assert {(c['category'], c['original_type']) for c in republished['definition_conflicts']} == old_conflicts


def test_label_conflict_across_snapshots_stays_visible_even_if_two_copies_agree(monkeypatch):
    first = [contributor(f'u{i}', _projection(i)) for i in (0, 1)]
    alternate = [(p, parent, c, 'Different name' if c == 'S01' else name, project)
                 for p, parent, c, name, project in ROWS]
    monkeypatch.setitem(globals(), 'ROWS', alternate)
    units = [*first, contributor('u2', _projection(2))]
    graph = merge_construction_graphs(units)
    assert sum(code(n) == 'S01' for n in graph['nodes']) == 3
    assert graph['fusion_diagnostics']['hierarchy_replay_diagnostics']['rejected_physical_sets']['multiple_labels'] == 1


@pytest.mark.parametrize('options', [
    {'business_filter': True}, {'other_context': True}, {'constants': {'edition': 'L2'}},
])
def test_scope_changing_filters_observations_or_constants_stay_separate(options):
    units = [contributor('one', _projection(0)), contributor('two', _projection(3, **options))]
    before = merge_construction_graphs(units, recognize_source_replays=False)
    after = merge_construction_graphs(units)
    assert {n['id'] for n in after['nodes']} == {n['id'] for n in before['nodes']}
    assert _facts(before) == _facts(after)


@pytest.mark.parametrize('ordinals,copies', [(None, 2), ({KEYS[0]: 2, KEYS[1]: 4}, 1)])
def test_partial_fold_depends_on_which_sheet_coordinates_the_checkpoint_can_verify(ordinals, copies):
    units = [contributor('left', _projection(0)), contributor('right', _projection(1)),
             contributor('separate', _projection(2, disputed_position=True))]
    # Some older checkpoints never recorded the physical header coordinate.
    for n in units[-1]['graph']['nodes']:
        for support in n['supports']:
            prov = support['provenance']
            prov.pop('source_column_ordinals', None)
            if ordinals is not None:
                prov['source_column_ordinals'] = {col: ordinals[col] for col in prov['key_columns']}
    before = merge_construction_graphs(units, recognize_source_replays=False)
    after = merge_construction_graphs(units)
    assert Counter(map(code, after['nodes'])) == dict.fromkeys(['CAT', 'ARC', 'S01', 'B01', 'S02'], copies)
    assert after['fusion_diagnostics']['hierarchy_replay_diagnostics']['physical_set_outcomes'] == [
        {'input_nodes': 3, 'output_nodes': copies, 'sets': 5}]
    if ordinals is None:
        assert after['fusion_diagnostics']['hierarchy_replay_diagnostics']['sets_by_observation_difference'] == {
            'properties': 5}
    assert _facts(before) == _facts(after)


def test_overlapping_physical_positions_cannot_extend_a_match_to_another_grain():
    # The same code occurs as a leaf in all three inputs, but only the last
    # two independently observed it in the parent slot as well.
    units = [contributor('one', _projection(0)), contributor('two', _projection(1, repeated_position=True)),
             contributor('three', _projection(2, repeated_position=True))]
    before = merge_construction_graphs(units, recognize_source_replays=False)
    after = merge_construction_graphs(units)
    assert {n['id'] for n in before['nodes'] if code(n) == 'S02'} == {
        n['id'] for n in after['nodes'] if code(n) == 'S02'}
    stats = after['fusion_diagnostics']['hierarchy_replay_diagnostics']
    assert stats['rejected_physical_sets']['overlapping_projection_sets'] >= 1
    assert _facts(before) == _facts(after)


def test_partial_fold_of_same_named_relations_preserves_edge_lineage_on_republication():
    units = []
    for i in range(3):
        g = _projection(i, conflict=True)
        for r in g['definitions']['relations']:
            r['type'] = 'stated_relation'
        for e in g['edges']:
            e['type'] = 'stated_relation'
            e['id'] = relation_identity(e)
        units.append(contributor(str(i), g))
    historical = merge_construction_graphs(units, recognize_source_replays=False)
    original = {e['id']: (e['type'], e['source'], e['target']) for e in historical['edges']}
    folded = merge_construction_graphs(units)
    assert len(folded['nodes']) == 7
    assert len(folded['edges']) == len(original) == 9
    for edge in folded['edges']:
        if edge['id'] not in original:
            for support in edge['supports']:
                previous = support['endpoint_origins'][-1]
                assert original[previous['edge_id']] == (previous['type'], previous['source'], previous['target'])
    frozen = merge_construction_graphs([{'kind': 'merged', 'contributor_id': 'published', 'graph': folded}])
    assert {e['id'] for e in frozen['edges']} == {e['id'] for e in folded['edges']}
    assert _facts(frozen) == _facts(historical)


@pytest.mark.parametrize('specialized', [False, True])
def test_partial_fold_respects_existing_noncanonical_edge_ids(specialized):
    units = [contributor(str(i), _projection(i, conflict=True)) for i in range(3)]
    folded = merge_construction_graphs(units)
    rewritten = next(e for e in folded['edges']
                     if (e['type'] != e.get('original_type', e['type'])) == specialized
                     and any(s.get('endpoint_origins') for s in e['supports']))
    # Older published edges can keep an ID minted under different conventions.
    # Both the original relation name and a specialized name can hit that ID.
    extra = contributor('legacy', draft_graph(99, 'Archive', source_id='another_source'))
    legacy = extra['graph']['edges'][0]
    legacy['id'] = rewritten['id']
    legacy.pop('business_context', None)
    result = merge_construction_graphs(units + [extra])
    assert len(result['edges']) == len({e['id'] for e in result['edges']}) == 11
    retained, = (e for e in result['edges'] if e['id'] == legacy['id'])
    assert (retained['type'], retained['source'], retained['target']) == (
        legacy['type'], legacy['source'], legacy['target'])
