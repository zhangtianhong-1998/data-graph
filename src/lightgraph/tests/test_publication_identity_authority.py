"""Independent publications need evidence tying their actual keys to the same identity."""
from copy import deepcopy

import pytest

from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workflow import load_unit_contributor
from lightgraph.construction.workspace import dump_yaml
from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource


def _source(*, west_code='001', shared_dictionary=False):
    tables = [TableSpec(source_id='db', name=t, columns=[ColumnSpec(name=c) for c in ('code', 'name', 'plant')])
              for t in ['east', 'west', 'weather']]
    if shared_dictionary:
        from lightgraph.contracts import ForeignKeySpec
        tables[0].unique_keys = [['code']]
        tables[1].foreign_keys = [ForeignKeySpec(name='east_assets', columns=['code'], target_schema='public',
                                                 target_table='east', target_columns=['code'])]
    rows = {t.qualified_name: [(west_code if t.name == 'west' else '001', '主泵', t.name)] for t in tables}
    return MemorySource(tables, rows)


def _draft(table, scope='unknown', ref=None):
    binding = {'source_id': 'db', 'table': table}
    entity = {'type': 'Asset', 'key_names': ['code'], 'identity_scope': scope, 'mappings': [
        {**binding, 'key_columns': ['code'], 'label_column': 'name', 'properties': {'plant': 'plant'}}]}
    if scope == 'global':
        entity.update(identity_domain='asset_catalog', identity_evidence_refs=[ref])
    return {'draft_id': table, 'entities': [entity,
        {'type': 'Plant', 'key_names': ['name'], 'mappings': [{**binding, 'key_columns': ['plant']}]}],
        'relations': [{'type': 'installed_at', 'source_type': 'Asset', 'target_type': 'Plant',
                       'mappings': [{**binding, 'source_key_columns': ['code'], 'target_key_columns': ['plant']}]}]}


def _publish(tools, draft):
    assert tools.call('save_graph_draft', yaml_text=dump_yaml(draft))['status'] == 'ok'
    assert tools.call('validate_graph_draft', draft_id=draft['draft_id'])['passed']
    assert tools.call('publish_graph_draft', draft_id=draft['draft_id'])['status'] == 'ok'


def _separate(graph):
    assets = {n['id']: n for n in graph['nodes'] if n['type'] == 'Asset'}
    plants = {n['id']: n['identity']['name'] for n in graph['nodes'] if n['type'] == 'Plant'}
    assert len(assets) == 2
    assert {(assets[e['source']]['properties']['plant'][0], plants[e['target']]) for e in graph['edges']} == {
        ('east', 'east'), ('west', 'west')}
    assert all(len(n['properties']['plant']) == 1 for n in assets.values())
    assert any(set(c['node_ids']) == assets.keys() for c in graph['candidate_identity_merges'])


def _config():
    return RunConfig(sources=[], model={'mode': 'in_environment'})


def test_same_workspace_unknown_collisions_keep_both_endpoints_and_offer_review(tmp_path):
    with _source() as source:
        tools = ConstructionTools(_config(), tmp_path / 'construction', adapters=[source])
        try:
            for table in ('west', 'east'):
                _publish(tools, _draft(table))
            _separate(tools.workspace.read_published_graph())
            old_ids = {n['id'] for n in tools.workspace.read_published_graph()['nodes']}
            assert tools.call('publish_graph_draft', draft_id='east')['status'] == 'ok'
            assert {n['id'] for n in tools.workspace.read_published_graph()['nodes']} == old_ids
        finally:
            tools.close()


def test_workspace_rechecks_catalog_evidence_for_snapshots_without_mapping_markers(tmp_path):
    with _source() as source:
        tools = ConstructionTools(_config(), tmp_path / 'construction', adapters=[source])
        try:
            ref = tools.call('inspect_source', table='db:public.east', operation='get_metadata')['evidence_ref']
            for owner in ('first', 'second'):
                draft = dict(_draft('east', 'global', ref), draft_id=owner)
                assert tools.call('save_graph_draft', yaml_text=dump_yaml(draft))['status'] == 'ok'
                assert tools.call('validate_graph_draft', draft_id=owner)['passed']
                current = tools.workspace.get_draft(owner)
                frozen = tools.workspace.read_validation_graph(owner, current.validation)
                asset = next(n for n in frozen['nodes'] if n['type'] == 'Asset')
                asset['supports'][0]['provenance'].pop('global_identity_authority')
                tools.workspace.record_validation(owner, passed=True, revision=current.revision, graph=frozen)
                published = tools.call('publish_graph_draft', draft_id=owner)
                assert published['status'] == 'ok', published
            graph = tools.workspace.read_published_graph()
            assert len([n for n in graph['nodes'] if n['type'] == 'Asset']) == 1
            assert len(graph['edges']) == 1
        finally:
            tools.close()


@pytest.mark.parametrize('shared_rule', [False, True])
def test_section_applicability_does_not_turn_a_whole_manual_into_one_registry(tmp_path, shared_rule):
    manual = tmp_path / 'dictionary.md'
    shared = '# Registry\nThe registry issues one globally unique asset code to both sites.\n' if shared_rule else ''
    manual.write_text('---\nid: assets\npurpose: data_description\n'
        'scope: {tables: [db.public.east, db.public.west], columns: [code]}\n'
        'sections:\n  East: {scope: {tables: [db.public.east]}}\n'
        '  West: {scope: {tables: [db.public.west]}}\n---\n' + shared +
        '# East\nThe code identifies the asset in the east plant catalog.\n'
        '# West\nThe code identifies the asset in the west plant catalog.\n', encoding='utf-8')
    config = _config().model_copy(update={'documents': [str(manual)]})
    units = []
    for table in ('east', 'west'):
        out = tmp_path / table
        with _source() as source:
            tools = ConstructionTools(config, out / 'construction', adapters=[source])
            try:
                metadata = tools.call('inspect_source', table='db:public.' + table, operation='get_metadata')
                refs = [metadata['evidence_ref']]
                page = 0
                while page is not None:
                    result = tools.call('read_material', name='document:assets', page=page)
                    if result['section']['heading'] in {table.title(), 'Registry'}:
                        refs.extend(result['evidence_refs'])
                    page = result['next_page']
                draft = _draft(table, 'global', refs[0])
                draft['entities'][0]['identity_evidence_refs'] = refs
                _publish(tools, draft)
            finally:
                tools.close()
        units.append(load_unit_contributor(out, table))
    graph = merge_construction_graphs([{**u, 'source_namespace': u['contributor_id']} for u in units])
    assert len([n for n in graph['nodes'] if n['type'] == 'Asset']) == (1 if shared_rule else 2)
    repeat = deepcopy(units[0])
    repeat['contributor_id'] = 'metadata_only'
    asset = next(n for n in repeat['graph']['nodes'] if n['type'] == 'Asset')
    asset['supports'][0]['provenance']['global_identity_authority'].pop('key_sections_v1')
    assert len([n for n in merge_construction_graphs([units[0], repeat])['nodes'] if n['type'] == 'Asset']) == 1
    # Old markers claimed the entire manual. Re-reading those markers cannot
    # establish a common section when independent physical addresses differ.
    for unit in units:
        asset = next(n for n in unit['graph']['nodes'] if n['type'] == 'Asset')
        marker = asset['supports'][0]['provenance']['global_identity_authority']
        marker.pop('key_sections_v1')
        marker['documents'] = ['whole-manual']
    _separate(merge_construction_graphs(units))


@pytest.mark.parametrize(('scope', 'basis', 'expected'), [
    ('global', 'independent', 0), ('unknown', 'independent', 0),
    ('global', 'different_codes', 2), ('unknown', 'same_table', 1),
    ('unknown', 'foreign_key', 1), ('global', 'explicit', 1)])
def test_multiple_tables_in_one_draft_need_evidence_for_observed_collisions(tmp_path, scope, basis, expected):
    config = _config()
    if basis == 'explicit':
        config = RunConfig(sources=[], model={'mode': 'in_environment'}, fusion_policy={'entity_rules': [
            {'entity_type': 'Asset', 'canonical_type': 'Asset', 'identity_domain': 'asset_catalog',
             'key_mapping': {'code': 'code'}}]})
    with _source(west_code='002' if basis == 'different_codes' else '001', shared_dictionary=basis == 'foreign_key') as source:
        tools = ConstructionTools(config, tmp_path / 'construction', adapters=[source])
        try:
            refs = [tools.call('inspect_source', table='db:public.' + t, operation='get_metadata')['evidence_ref']
                    for t in ('east', 'west')]
            draft = _draft('east', scope, refs[0])
            entity = draft['entities'][0]
            if scope == 'global':
                entity['identity_evidence_refs'] = refs
            entity['mappings'].append({**entity['mappings'][0], 'table': 'east' if basis == 'same_table' else 'west'})
            assert tools.call('save_graph_draft', yaml_text=dump_yaml(draft))['status'] == 'ok'
            checked = tools.call('validate_graph_draft', draft_id='east')
            assert checked['passed'] is bool(expected), checked
            if expected:
                assert tools.call('publish_graph_draft', draft_id='east')['status'] == 'ok'
                assert len([n for n in tools.workspace.read_published_graph()['nodes'] if n['type'] == 'Asset']) == expected
            else:
                diagnostic, = checked['diagnostics']
                assert diagnostic['code'] == 'cross_table_identity_unverified'
                assert (diagnostic['entity_type'], diagnostic['mappings'], diagnostic['affected_nodes']) == ('Asset', [0, 1], 1)
                # Even a historical validation that marked the same graph passed
                # must be rechecked before publishing its frozen observations.
                current = tools.workspace.get_draft('east')
                frozen = tools.workspace.read_validation_graph('east', current.validation)
                tools.workspace.record_validation('east', revision=current.revision, passed=True, graph=frozen)
                assert tools.call('publish_graph_draft', draft_id='east')['code'] == 'cross_table_identity_unverified'
        finally:
            tools.close()


def test_reusing_immutable_previously_successful_validation_rechecks_cross_table_collisions(tmp_path):
    sources = []
    mappings = []
    for site in ('east', 'west'):
        path = tmp_path / (site + '.csv')
        path.write_text('code,name\n001,pump\n', encoding='utf-8')
        sources.append({'id': site, 'kind': 'csv_file', 'path': str(path), 'encoding': 'utf-8', 'delimiter': ',',
                        'header': True, 'null_values': [''], 'type_inference': 'text', 'table_name': 'assets'})
        mappings.append({'source_id': site, 'table': 'assets', 'key_columns': ['code'], 'label_column': 'name'})
    config = RunConfig(sources=sources, model={'mode': 'in_environment'})
    tools = ConstructionTools(config, tmp_path / 'construction')
    try:
        draft = {'draft_id': 'legacy', 'entities': [{'type': 'Asset', 'key_names': ['code'], 'mappings': mappings}]}
        assert tools.call('save_graph_draft', yaml_text=dump_yaml(draft))['status'] == 'ok'
        assert not tools.call('validate_graph_draft', draft_id='legacy')['passed']
        record = tools.workspace.get_draft('legacy')
        frozen = tools.workspace.read_validation_graph('legacy', record.validation)
        tools.workspace.record_validation('legacy', revision=record.revision, passed=True, graph=frozen)
        result = tools.call('validate_graph_draft', draft_id='legacy', reuse_validated=True)
        assert not result['passed'] and result['code'] == 'cross_table_identity_unverified'
    finally:
        tools.close()


def test_global_evidence_must_cover_each_mapped_table_and_does_not_turn_two_catalogs_into_one(tmp_path):
    units = []
    for table in ('east', 'west'):
        out = tmp_path / table
        with _source() as source:
            tools = ConstructionTools(_config(), out / 'construction', adapters=[source])
            try:
                def metadata(name):
                    return tools.call('inspect_source', table='db:public.' + name, operation='get_metadata')['evidence_ref']
                rejected = tools.call('save_graph_draft', yaml_text=dump_yaml(_draft(table, 'global', metadata('weather'))))
                assert rejected['code'] == 'identity_evidence_not_applicable'
                assert rejected['diagnostics'][0]['columns'] == ['code']
                mixed = _draft(table, 'global', metadata(table))
                mapping = mixed['entities'][0]['mappings'][0]
                mixed['entities'][0]['mappings'].append({**mapping, 'table': 'weather'})
                rejected = tools.call('save_graph_draft', yaml_text=dump_yaml(mixed))
                assert rejected['code'] == 'identity_evidence_not_applicable'
                assert rejected['diagnostics'][0]['table'] == 'weather'
                _publish(tools, _draft(table, 'global', metadata(table)))
            finally:
                tools.close()
        units.append(load_unit_contributor(out, table))
    split = merge_construction_graphs(units)
    _separate(split)
    first = {**units[0], 'source_namespace': 'one'}
    for namespace, count in [('one', 1), ('two', 2), (None, 2)]:
        repeat = {**units[0], 'contributor_id': 'repeat', 'source_namespace': namespace}
        separate = merge_construction_graphs([first, repeat])
        assert len([n for n in separate['nodes'] if n['type'] == 'Asset']) == count
    replay = merge_construction_graphs([{'kind': 'merged', 'contributor_id': 'prior', 'graph': split},
                                        {**units[0], 'contributor_id': 'repeat_east'}])
    original_ids = {n['id'] for n in split['nodes'] if n['type'] == 'Asset'}
    assert {n['id'] for n in replay['nodes'] if n['type'] == 'Asset'} == original_ids
    assert merge_construction_graphs(units) == merge_construction_graphs(units[::-1])
    # A previously accepted declaration may cite an unrelated table. Replaying
    # that historical graph cannot authorize new cross-source identity links.
    for unit in units:
        for n in unit['graph']['nodes']:
            for s in n['supports']:
                s['provenance'].pop('global_identity_authority', None)
        for ref in unit['evidence_records'].values():
            if ref['kind'] == 'source_probe':
                ref['source_ref']['table'] = 'weather'
    _separate(merge_construction_graphs(units))
    _separate(merge_construction_graphs([{**units[0], 'kind': 'base'}, units[1]]))
    for unit in units:
        asset = next(n for n in unit['graph']['nodes'] if n['type'] == 'Asset')
        asset['publication_identity_original_id'] = 'old_unqualified_id'
    _separate(merge_construction_graphs(units))
    explicit = merge_construction_graphs(units, merge_policy={'shared_entity_types': ['Asset']})
    assert len([n for n in explicit['nodes'] if n['type'] == 'Asset']) == 1


@pytest.mark.parametrize('missing', ['other_table', 'unreferenced', 'inherited', 'no_scope'])
def test_historical_catalog_probe_must_cover_every_observation(tmp_path, missing):
    out = tmp_path / 'old'
    with _source() as source:
        tools = ConstructionTools(_config(), out / 'construction', adapters=[source])
        try:
            ref = tools.call('inspect_source', table='db:public.east', operation='get_metadata')['evidence_ref']
            _publish(tools, _draft('east', 'global', ref))
        finally:
            tools.close()
    unit = load_unit_contributor(out, 'first')
    node = next(n for n in unit['graph']['nodes'] if n['type'] == 'Asset')
    support, = node['supports']
    support['provenance'].pop('global_identity_authority')
    repeat = deepcopy(unit)
    repeat['contributor_id'] = 'second'

    def assets():
        return [n for n in merge_construction_graphs([unit, repeat])['nodes'] if n['type'] == 'Asset']

    assert len(assets()) == 1
    extra = deepcopy(support)
    if missing == 'other_table':
        extra['provenance']['source']['table'] = 'weather'
    elif missing == 'unreferenced':
        extra['evidence_refs'] = []
    elif missing == 'inherited':
        extra['inherited_from'] = 'another_project'
    else:
        extra.pop('scope_ref')
    node['supports'].append(extra)
    assert len(assets()) == 2


@pytest.mark.parametrize('conflict', ['project', 'scope', 'table'])
def test_unproven_observation_cannot_join_an_existing_same_id_replay(conflict):
    from lightgraph.construction.workspace import _merge_graphs
    from lightgraph.tests.test_observed_identity import projection
    good = projection('Term', 'code')
    other = deepcopy(good)
    for n in other['nodes']:
        support, = n['supports']
        if conflict == 'scope':
            support['scope_ref'] = 'restricted'
        elif conflict == 'table':
            support['provenance']['source']['table'] = 'independent'
        else:
            support['business_context']['dimensions']['project'] = 'other'
    units = [dict(good, draft_id='first'), dict(good, draft_id='second'), dict(other, draft_id='third')]
    merged = _merge_graphs(units)
    assert len(merged['nodes']) == 4 and len(merged['edges']) == 2
    assert len(merged['candidate_identity_merges']) == 2
