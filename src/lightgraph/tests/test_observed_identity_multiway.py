"""Multi-branch replay from actual CSV-backed unit publications."""
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import dump_yaml
from lightgraph.tests.test_construction_tools import config_for


def test_unrelated_type_owner_does_not_veto_two_units_replaying_the_same_rows(tmp_path):
    branches = []
    for contributor, source in [('first', 'catalogue'), ('second', 'catalogue'), ('third', 'other')]:
        config = config_for(tmp_path)
        config.sources[0].id = source
        (tmp_path / 'input.csv').write_text('code,title,parent\na,Book A,\nb,Book B,a\n', encoding='utf-8')
        config.sources[0].table_name = 'objects'
        tools = ConstructionTools(config, tmp_path / contributor)
        try:
            mapping = {'source_id': source, 'schema': 'public', 'table': 'objects'}
            proposal = {
                'draft_id': 'catalogue_entries',
                'entities': [{'type': 'Record', 'key_names': ['code'], 'mappings': [
                    {**mapping, 'key_columns': ['code'], 'label_column': 'title'}]}],
                'relations': [{'type': 'belongs_to', 'source_type': 'Record', 'target_type': 'Record',
                               'mappings': [{**mapping, 'source_key_columns': ['code'],
                                             'target_key_columns': ['parent'],
                                             'predicate': {'op': 'is_not_null', 'column': 'parent'}}]}],
            }
            assert tools.call('save_graph_draft', yaml_text=dump_yaml(proposal))['status'] == 'ok'
            checked = tools.call('validate_graph_draft', draft_id='catalogue_entries')
            assert checked['passed'], checked
            assert tools.call('publish_graph_draft', draft_id='catalogue_entries')['status'] == 'ok'
            branches.append({'contributor_id': contributor, 'graph': tools.workspace.read_published_graph()})
        finally:
            tools.close()

    paired = merge_construction_graphs(branches[:2])
    assert len(paired['nodes']) == 2 and len(paired['edges']) == 1
    merged = merge_construction_graphs(branches)
    assert len(merged['nodes']) == 4 and len(merged['edges']) == 2
    assert merged == merge_construction_graphs(list(reversed(branches)))
    by_id = {node['id']: node for node in merged['nodes']}
    assert set(by_id) >= {node['id'] for node in paired['nodes']}
    for edge in merged['edges']:
        # Identical names and row keys in the other catalogue keep their own edge.
        source_sets = [{s['provenance']['source']['source_id'] for s in by_id[edge[end]]['supports']}
                       for end in ('source', 'target')]
        assert source_sets[0] == source_sets[1] and len(source_sets[0]) == 1
        contributors = {origin['contributor_id'] for s in by_id[edge['source']]['supports']
                        for origin in s['execution_origins']}
        assert contributors == ({'first', 'second'} if source_sets[0] == {'catalogue'} else {'third'})


def test_two_competing_roles_cannot_mint_the_same_replay_id():
    from lightgraph.tests.test_observed_identity import projection, unit

    branches = []
    for index, role in enumerate(['requested', 'requested', 'fulfilled', 'fulfilled']):
        graph = projection()
        graph['edges'][0]['supports'][0]['provenance'].update(
            source_key_columns=[role + '_parent'], target_key_columns=[role + '_child'])
        branches.append(unit(str(index), graph))
    result = merge_construction_graphs(branches)
    assert len(result['nodes']) == 8
    assert len(result['edges']) == 4
