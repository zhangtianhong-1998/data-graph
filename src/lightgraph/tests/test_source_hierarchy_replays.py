"""A deterministic merge of separately published views of a physical hierarchy."""
from collections import Counter
from copy import deepcopy

import pytest

from lightgraph.construction.executor import execute_draft
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.construction.workspace import _merge_graphs
from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.sources import MemorySource


def draft_graph(number, kind, *, inverse=False, source_id='file', project=None, alternate_role=False,
                declare_direction=True, self_policy=None, collection=None, positions=None, code_prefix=''):
    logical_key = 'accession' if number % 2 else 'item_code'
    columns = ['section', 'caption', 'leaf', 'heading', 'cross_reference']
    table = TableSpec(source_id=source_id, name='vocabulary', columns=[ColumnSpec(name=c) for c in columns])
    rows = [('X', 'Catalogue', 'A-01', 'Inscription', 'A-01'),
            ('X', 'Catalogue', 'B-01', 'Inscription', 'B-01')]
    if self_policy:
        rows.append(('X', 'Catalogue', 'X', 'Catalogue', 'X'))
    if code_prefix:
        rows = [(code_prefix + parent, caption, code_prefix + child, label, code_prefix + other)
                for parent, caption, child, label, other in rows]
    binding = {'source_id': source_id, 'table': table.name}
    if project:
        binding['applicability'] = {'status': 'scoped', 'values': {'collection': project}}
    endpoints = ['section', 'cross_reference' if alternate_role else 'leaf']
    if inverse:
        endpoints.reverse()
    draft = {
        'draft_id': f'draft{number}',
        'entities': [{'type': kind, 'key_names': [logical_key], 'identity_scope': 'global',
                      'identity_domain': f'namespace {number}', 'identity_evidence_refs': [f'e{number}'],
                      'mappings': [{**binding, 'key_columns': [key], 'label_column': label,
                                    'constant_properties': {
                                        **({'collection': collection} if collection is not None else {}),
                                        **({'slot': positions[i]} if positions is not None else {})}}
                                   for i, (key, label) in enumerate([('section', 'caption'), ('leaf', 'heading')])]}],
        'relations': [{'type': f'link{number}', 'source_type': kind, 'target_type': kind,
                       **({'hierarchy_orientation': 'child_to_parent' if inverse else 'parent_to_child'}
                          if declare_direction else {}),
                       'allow_self_links': self_policy == 'include',
                       'mappings': [{**binding, 'source_key_columns': [endpoints[0]],
                                     'equal_endpoints': self_policy or 'error',
                                     'target_key_columns': [endpoints[1]]}]}],
    }
    with MemorySource([table], {table.qualified_name: rows}) as adapter:
        result = execute_draft(draft, [adapter], [table])
    assert result['passed'], result['diagnostics']
    graph = result['graph']
    graph.update(draft_id=draft['draft_id'], draft_revision=1)
    return graph


def contributor(name, *drafts, evidence_kind='source_probe'):
    records = {ref: {'kind': evidence_kind, 'source_ref': 'file.public.vocabulary'}
               for graph in drafts for entity in graph['definitions']['entities']
               for ref in entity.get('identity_evidence_refs', [])}
    return {'contributor_id': name, 'graph': _merge_graphs(drafts), 'evidence_records': records}


def code(node):
    value, = node['identity'].values()
    return value


def edge_facts(graph):
    nodes = {n['id']: n for n in graph['nodes']}
    return Counter((e['type'], code(nodes[e['source']]), code(nodes[e['target']]),
                    s['scope_ref'], tuple(s['provenance']['source_key_columns']),
                    tuple(s['provenance']['target_key_columns']))
                   for e in graph['edges'] for s in e['supports'])


def test_five_global_projections_share_the_physical_members_and_keep_both_directions():
    units = [contributor('first', draft_graph(1, 'Entry'), draft_graph(2, 'Entry', inverse=True,
                                                                      declare_direction=False)),
             contributor('second', draft_graph(3, 'Heading')),
             contributor('third', draft_graph(4, 'Subject')),
             contributor('fourth', draft_graph(5, 'Term'))]
    historical = deepcopy(units)
    baseline = merge_construction_graphs(units, recognize_source_replays=False)
    assert (len(baseline['nodes']), len(baseline['edges'])) == (15, 10)
    graph = merge_construction_graphs(units)
    assert units == historical
    assert graph == merge_construction_graphs(list(reversed(units)))
    assert sorted(map(code, graph['nodes'])) == ['A-01', 'B-01', 'X']
    assert len(graph['edges']) == 10
    assert edge_facts(graph) == edge_facts(baseline)
    assert all(e['source'] != e['target'] for e in graph['edges'])
    definitions = {r['type']: r for r in graph['definitions']['relations']}
    by_id = {n['id']: n for n in graph['nodes']}
    for edge in graph['edges']:
        for end in ('source', 'target'):
            assert definitions[edge['type']][end + '_type'] == by_id[edge[end]]['type']
    for node in graph['nodes']:
        origins = [origin for support in node['supports'] for origin in support.get('identity_origins', [])]
        assert {o.get('identity_domain') for o in origins} == {f'namespace {i}' for i in range(1, 6)}
        assert {next(iter(o['identity'])) for o in origins} == {'accession', 'item_code'}
        assert all(s['scope_ref'] == 'public' for s in node['supports'])


@pytest.mark.parametrize('difference', ['physical_source', 'mixed_sources', 'private', 'project',
                                        'different_role', 'documented_domain',
                                        'foreign_base', 'no_relation_witness', 'undeclared_roles',
                                        'opposite_role', 'ordinary_relation'])
def test_identical_labels_and_codes_never_override_an_incompatible_source_role_or_domain(difference):
    declare = difference != 'undeclared_roles'
    first = contributor('one', draft_graph(1, 'Heading', project='M' if difference == 'project' else None,
                                            declare_direction=declare))
    second = contributor('two', draft_graph(
        2, 'Term', project='K' if difference == 'project' else None,
        source_id='other' if difference == 'physical_source' else 'file',
        alternate_role=difference == 'different_role', declare_direction=declare),
        evidence_kind='document_section' if difference == 'documented_domain' else 'source_probe')
    if difference == 'foreign_base':
        first['kind'] = 'base'
    if difference == 'no_relation_witness':
        second['graph']['edges'].clear()
    for node in second['graph']['nodes']:
        if difference == 'mixed_sources':
            extra = deepcopy(node['supports'][0])
            extra['provenance']['source']['table'] = 'another_vocabulary'
            node['supports'].append(extra)
        if difference == 'private':
            for support in node['supports']:
                support['required_scope_refs'] = ['restricted_collection']
    if difference in {'opposite_role', 'ordinary_relation'}:
        second['graph']['definitions']['relations'][0]['hierarchy_orientation'] = (
            'none' if difference == 'ordinary_relation' else 'child_to_parent')
    merged = merge_construction_graphs([first, second])
    assert len(merged['nodes']) == 6
    assert Counter(map(code, merged['nodes'])) == {'X': 2, 'A-01': 2, 'B-01': 2}


def test_self_links_retained_by_only_one_projection_survive_with_their_evidence():
    units = [contributor('one', draft_graph(1, 'Heading', self_policy='include')),
             contributor('two', draft_graph(2, 'Term', inverse=True, self_policy='omit'))]
    before = merge_construction_graphs(units, recognize_source_replays=False)
    after = merge_construction_graphs(units)
    assert len(after['nodes']) == 3
    assert edge_facts(after) == edge_facts(before)
    loop, = (e for e in after['edges'] if e['source'] == e['target'])
    assert loop['type'] == 'link1'


@pytest.mark.parametrize('other_collection,expected_count', [('A', 3), ('B', 6)])
def test_source_level_positions_do_not_override_constant_collection_boundaries(other_collection, expected_count):
    units = [contributor('one', draft_graph(1, 'Heading', collection='A', positions=['section', 'leaf'])),
             contributor('two', draft_graph(2, 'Term', inverse=True, collection=other_collection))]
    graph = merge_construction_graphs(units)
    assert len(graph['nodes']) == expected_count
    for node in graph['nodes']:
        levels = [v for s in node['supports'] for v in s['properties'].get('slot', [])]
        if other_collection == 'A':
            assert set(levels) == ({'section'} if code(node) == 'X' else {'leaf'})
        assert len({v for s in node['supports'] for v in s['properties']['collection']}) == 1


def test_project_labels_that_vary_with_source_position_do_not_establish_that_they_mean_level():
    one = contributor('one', draft_graph(1, 'Heading', positions=['M', 'K']))
    two = contributor('two', draft_graph(2, 'Term', positions=['K', 'M']))
    assert len(merge_construction_graphs([one, two])['nodes']) == 6


def test_direction_declared_in_another_collection_for_other_values_cannot_authorize_ordinary_relations():
    local = [contributor('one', draft_graph(1, 'Heading', declare_direction=False, project='M')),
             contributor('two', draft_graph(2, 'Term', declare_direction=False, project='M'))]
    other = contributor('other', draft_graph(3, 'Edition', code_prefix='other-', project='K'))
    graph = merge_construction_graphs([*local, other])
    assert len(graph['nodes']) == 9


def test_unrelated_pending_relation_does_not_prevent_reconciling_the_clean_hierarchy():
    clean = [contributor('one', draft_graph(1, 'Heading')), contributor('two', draft_graph(2, 'Term'))]
    third = contributor('three', draft_graph(3, 'Edition', source_id='other'))
    external = third['graph']
    pending = deepcopy(external['edges'][0])
    pending.update(id='r_pending', status='unresolved', node_ids=[pending['source']],
                   candidate_endpoints={'source': [pending['source']], 'target': []})
    del pending['source'], pending['target']
    external['unresolved_relations'] = [pending]
    external['conflict_isolation'] = {'coverage': 'incomplete', 'node_ids': ['isolated']}
    graph = merge_construction_graphs(clean + [third])
    assert len(graph['nodes']) == 6
    assert graph['conflict_isolation']['coverage'] == 'incomplete'
    record, = graph['unresolved_relations']
    assert record['id'] == 'r_pending'
    assert set(record['node_ids']) <= {n['id'] for n in graph['nodes'] if n['type'] == 'Edition'}
