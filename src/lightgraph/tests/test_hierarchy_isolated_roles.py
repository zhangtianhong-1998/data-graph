"""A disconnected source member can have a verifiable role without acquiring fictitious edges."""
from collections import Counter

import pytest

from lightgraph.construction.executor import execute_draft
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.construction.workspace import _merge_graphs
from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.runtime.config import FusionPolicyConfig
from lightgraph.sources import MemorySource
from lightgraph.tests.test_hierarchy_projection_variants import _facts
from lightgraph.tests.test_source_hierarchy_replays import code, contributor


def _unit(kind, *, anchor=('P', 'C'), self_link=False, project='library', anchor_project=None,
          extra_mapping=False, inverse=False, extra=None, solo_label='Special', unversioned=False):
    table = TableSpec(source_id='archive', name='terms', columns=[ColumnSpec(name=c) for c in
                      ('upper', 'upper_title', 'lower', 'lower_title', 'collection')])
    rows = [('S', solo_label, 'S', solo_label, project)]
    for pair in (anchor, extra):
        if pair:
            rows.append((pair[0], 'Parent', pair[1], 'Child', anchor_project or project))
    source = {'source_id': 'archive', 'table': 'terms', 'context_columns': ['upper', 'lower', 'collection']}
    mappings = []
    for key in ('upper', 'lower'):
        m = {**source, 'key_columns': [key], 'label_column': key + '_title'}
        if extra_mapping:
            # Separate mappings project the sample edge and the singleton; a
            # validation of the former cannot be reused for the latter.
            mappings.extend([{**m, 'predicate': {'column': key, 'op': comparator, 'value': 'S'}}
                             for comparator in ('eq', 'ne')])
        else:
            mappings.append(m)
    ends = ('lower', 'upper') if inverse else ('upper', 'lower')
    draft = {'draft_id': kind, 'entities': [{'type': kind, 'key_names': ['key'],
             'identity_scope': 'global', 'identity_domain': kind, 'identity_evidence_refs': ['e1'],
             'mappings': mappings}],
             'relations': [{'type': 'relation' + kind, 'source_type': kind, 'target_type': kind,
             'allow_self_links': self_link, 'hierarchy_orientation': 'child_to_parent' if inverse else 'parent_to_child',
             'mappings': [{**source, 'source_key_columns': [ends[0]], 'target_key_columns': [ends[1]],
                           'equal_endpoints': 'include' if self_link else 'omit'}]}]}
    with MemorySource([table], {table.qualified_name: rows}) as src:
        result = execute_draft(draft, [src], [table])
    assert result['passed'], result['diagnostics']
    published = dict(result['graph'], draft_id=kind, draft_revision=1)
    if unversioned:
        published.pop('draft_revision')
    unit = contributor(kind, published)
    if not unversioned:
        unit['graph'] = _merge_graphs([published], fusion_policy=FusionPolicyConfig())
    return unit


def _check(units, copies):
    before = merge_construction_graphs(units, recognize_source_replays=False)
    graph = merge_construction_graphs(units)
    assert Counter(map(code, graph['nodes']))['S'] == copies
    assert _facts(graph) == _facts(before)
    ids = {n['id'] for n in graph['nodes'] if code(n) == 'S'}
    incident = [e for e in graph['edges'] if e['source'] in ids or e['target'] in ids]
    assert all(e['source'] == e['target'] for e in incident)
    return graph, incident


def test_shared_source_mapping_confirms_a_singleton_and_preserves_its_existing_self_link():
    graph, incident = _check([_unit('Entry', self_link=True), _unit('Term', inverse=True), _unit('Work')], 1)
    assert len(incident) == 1
    assert Counter(map(code, graph['nodes'])) == {'P': 1, 'C': 1, 'S': 1}
    assert graph['fusion_diagnostics']['hierarchy_replay_diagnostics']['isolated_members_by_role_result'] == {
        'passed_role_check': 3}


@pytest.mark.parametrize('first,second', [
    ({'self_link': True, 'anchor': None}, {'self_link': True, 'anchor': None}),
    ({}, {'anchor': ('P', 'D')}),
    ({'anchor_project': 'elsewhere'}, {'anchor_project': 'elsewhere'}),
    ({}, {'project': 'other'}),
    ({'extra_mapping': True}, {'extra_mapping': True}),
    ({}, {'unversioned': True}),
    ({}, {'solo_label': 'Other meaning'}),
])
def test_isolated_member_needs_matching_role_and_source_evidence(first, second):
    _check([_unit('Entry', **first), _unit('Term', **second)], 2)


def test_pairwise_role_overlap_cannot_authorize_a_three_unit_union():
    _check([_unit('Entry'), _unit('Work', extra=('X', 'Y')), _unit('Term', anchor=('X', 'Y'))], 3)


def test_independent_unrelated_projection_cannot_veto_the_proven_pair():
    graph, _ = _check([_unit('Entry'), _unit('Work'), _unit('Term', anchor=('X', 'Y'))], 2)
    assert graph['fusion_diagnostics']['hierarchy_replay_diagnostics']['isolated_members_by_role_result'] == {
        'passed_role_check': 2, 'no_common_role_witness': 1}
