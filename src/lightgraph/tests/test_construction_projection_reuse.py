"""Share complete observations, never infer relations from unrelated values."""
from copy import deepcopy

from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.construction.executor import execute_draft
from lightgraph.construction.models import COLUMN_IDENTITY_NAMES
from lightgraph.tests.test_construction_projection_lifecycle import TrackedSource


def example(count=2, *, basis='schema_applies_to_rows'):
    columns = ['indicator', 'scope'] + [f'measure_{i}' for i in range(count)]
    table = TableSpec(source_id='s', name='facts', columns=[ColumnSpec(name=c) for c in columns])
    base = {'source_id': 's', 'table': 'facts'}
    draft = {'draft_id': 'analysis', 'entities': [
        {'type': 'Indicator', 'key_names': ['code'], 'mappings': [{**base, 'key_columns': ['indicator']}]},
        {'type': 'Measure', 'key_names': COLUMN_IDENTITY_NAMES, 'mappings': [
            {**base, 'kind': 'metadata_column', 'column': c, 'evidence_refs': [f'e{i}']}
            for i, c in enumerate(columns[2:])]}],
        'relations': [{'type': 'uses', 'source_type': 'Indicator', 'target_type': 'Measure', 'mappings': [
            {**base, 'source_key_columns': ['indicator'], 'target_metadata': {
                **base, 'column': c, 'evidence_refs': [f'e{i}']}, 'metadata_basis': basis}
            for i, c in enumerate(columns[2:])]}]}
    return table, draft


def pairs(result):
    nodes = {n['id']: n for n in result['graph']['nodes']}
    return {(nodes[e['source']]['identity']['code'], nodes[e['target']]['identity']['column'])
            for e in result['graph']['edges']}


def test_many_schema_relations_reuse_one_complete_read_and_keep_each_column_support():
    table, draft = example(37)
    values = [('I1', 'A', *([10] * 37)), ('I2', 'B', *([None] * 37))]
    with TrackedSource([table], {table.qualified_name: values}, activity=[]) as source:
        result = execute_draft(draft, [source], [table])
        assert result['passed'], result['diagnostics']
        assert len(source.windows) == 1 and len(source.windows[0]) == 2
        assert source.stats.queries == 2
        assert result['metrics']['unique_projections'] == 1
        assert result['metrics']['projection_cache_hits'] == 37
        assert len(result['graph']['nodes']) == 39
        assert pairs(result) == {(code, f'measure_{i}') for code in ('I1', 'I2') for i in range(37)}
        nodes = {n['id']: n for n in result['graph']['nodes']}
        for edge in result['graph']['edges']:
            column = nodes[edge['target']]['identity']['column']
            support, = edge['supports']
            endpoint = support['provenance']['metadata_endpoint']
            assert endpoint['column']['column'] == column
            assert endpoint['column']['evidence_refs'] == ['e' + column.rsplit('_', 1)[1]]
            assert endpoint['basis'] == 'schema_applies_to_rows'
            assert endpoint['row_membership'] == 'observed_projection'
            assert endpoint['non_null_observed'] is False
        assert len(result['source_read_windows']) == 1


def test_non_null_column_predicates_do_not_share_unrelated_observations():
    table, draft = example(basis='observed_non_null')
    values = [('I1', 'A', 1, None), ('I2', 'B', None, 2)]
    with TrackedSource([table], {table.qualified_name: values}, activity=[]) as source:
        result = execute_draft(draft, [source], [table])
        assert result['passed'], result['diagnostics']
        assert pairs(result) == {('I1', 'measure_0'), ('I2', 'measure_1')}
        assert len(source.windows) == 3 and source.stats.queries == 6
        assert result['metrics']['projection_cache_hits'] == 0


def test_explicit_predicates_and_row_context_keep_different_projections():
    table, draft = example()
    mappings = draft['relations'][0]['mappings']
    for number, mapping in enumerate(mappings):
        mapping['predicate'] = {'column': 'scope', 'op': 'eq', 'value': ['A', 'B'][number]}
        mapping['context_columns'] = ['scope']
    with TrackedSource([table], {table.qualified_name: [('I1', 'A', 1, 2), ('I2', 'B', 1, 2)]}, activity=[]) as source:
        result = execute_draft(draft, [source], [table])
        assert result['passed'], result['diagnostics']
        assert pairs(result) == {('I1', 'measure_0'), ('I2', 'measure_1')}
        assert len(source.windows) == 3
        assert {edge['supports'][0]['row_context']['values']['scope']
                for edge in result['graph']['edges']} == {'A', 'B'}


def test_endpoint_authorization_is_checked_even_with_matching_cached_rows():
    table, draft = example()
    draft['relations'][0]['mappings'][1]['target_metadata']['column'] = 'not_authorized'
    draft['entities'][1]['mappings'][1]['column'] = 'not_authorized'
    with TrackedSource([table], {table.qualified_name: [('I1', 'A', 1, 2)]}, activity=[]) as source:
        result = execute_draft(draft, [source], [table])
        assert not result['passed']
        assert pairs(result) == {('I1', 'measure_0')}
        assert any(d['code'] == 'unauthorized_column' and d.get('mapping') == 1
                   for d in result['diagnostics'])
        assert len(source.windows) == 1 and result['metrics']['projection_cache_hits'] == 1


def test_same_rows_in_other_source_cannot_reuse_a_complete_observation():
    first, draft = example(1)
    second = first.model_copy(update={'source_id': 'other'})
    for entity in draft['entities']:
        added = deepcopy(entity['mappings'][0]); added['source_id'] = 'other'
        entity['mappings'].append(added)
    added = deepcopy(draft['relations'][0]['mappings'][0])
    added['source_id'] = added['target_metadata']['source_id'] = 'other'
    draft['relations'][0]['mappings'].append(added)
    with TrackedSource([first], {first.qualified_name: [('I1', 'A', 1)]}, activity=[]) as a, TrackedSource(
            [second], {second.qualified_name: [('I2', 'B', 2)]}, activity=[]) as b:
        result = execute_draft(draft, [a, b], [first, second])
        assert result['passed'], result['diagnostics']
        assert len(a.windows) == len(b.windows) == 1
        assert result['metrics']['unique_projections'] == 2
        assert len(result['graph']['edges']) == 2
        nodes = {n['id']: n for n in result['graph']['nodes']}
        assert {(nodes[e['source']]['identity']['code'], nodes[e['target']]['identity']['source_id'])
                for e in result['graph']['edges']} == {('I1', 's'), ('I2', 'other')}


def test_incomplete_projection_is_never_reused_for_later_column_relations():
    class IncompleteSource(TrackedSource):
        def project(self, table, columns, **options):
            options['limit'] = 1
            yield from super().project(table, columns, **options)

    table, draft = example()
    with IncompleteSource([table], {table.qualified_name: [('I1', 'A', 1, 2), ('I2', 'B', 3, 4)]}, activity=[]) as source:
        result = execute_draft(draft, [source], [table])
        assert not result['passed'] and not result['graph']['edges']
        assert result['metrics']['projection_cache_hits'] == 0
        assert result['metrics']['unique_projections'] == 0
        assert len(source.windows) == 3
        assert [d['code'] for d in result['diagnostics']] == ['result_truncated'] * 3
