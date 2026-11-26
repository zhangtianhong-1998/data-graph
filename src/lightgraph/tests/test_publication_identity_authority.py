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


def _read_identity_refs(tools, table, headings, document='assets'):
    metadata = tools.call('inspect_source', table='db:public.' + table, operation='get_metadata')
    refs = [metadata['evidence_ref']]
    page = 0
    while page is not None:
        result = tools.call('read_material', name='document:' + document, page=page)
        if result['section']['heading'] in headings:
            refs.extend(result['evidence_refs'])
        page = result['next_page']
    return refs


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
                refs = _read_identity_refs(tools, table, {table.title(), 'Registry'})
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


def independent_projection_inputs(tmp_path, document_case, *, same_type=False, document_access=None):
    from lightgraph.construction.source_namespaces import project_checkpoint_inputs
    manual = tmp_path / 'assets.md'
    manual.write_text(
        '---\nid: assets\npurpose: data_description\n'
        'scope: {tables: [db.public.east, db.public.west], columns: [code]}\n'
        'sections:\n  East: {scope: {tables: [db.public.east]}}\n'
        '  West: {scope: {tables: [db.public.west]}}\n---\n'
        '# Registry\nVersion 3: code identifies one asset across both plants.\n'
        '# East\nLocal asset code in the east listing.\n# West\nLocal asset code in the west listing.\n',
        encoding='utf-8')
    config = _config().model_copy(update={'documents': [str(manual)]})
    independent = document_case.startswith('independent_')
    columns_in_key = ['code', 'plant'] if 'compound' in document_case else ['code']
    if document_case == 'independent_no_records':
        config = config.model_copy(update={'documents': []})
    elif independent:
        paths = []
        for table in ('east', 'west'):
            path = tmp_path / f'{table}.md'
            registry = {'issuer': 'asset-board', 'catalog': 'equipment', 'version': 'v3', 'key_roles': ['asset']}
            if 'compound' in document_case:
                registry['key_roles'] = ['asset', 'site']
                if table == 'west' and document_case == 'independent_compound_swapped':
                    registry['key_roles'].reverse()
            if table == 'west':
                for field in ('issuer', 'catalog', 'version', 'roles'):
                    if document_case == 'independent_' + field:
                        registry['key_roles' if field == 'roles' else field] = ['facility'] if field == 'roles' else 'other'
            contract = {'domain': f'{table} field directory', 'scope': 'shared',
                        'key_columns': {f'db:public.{table}': columns_in_key}}
            if document_case != 'independent_no_registry':
                contract['registry'] = registry
            sections = {'Registry': {'identity_contract': contract}}
            if table == 'west' and document_case == 'independent_conflict':
                sections['Exception'] = {'identity_contract': {**contract, 'registry': {**registry, 'version': 'v1'}}}
            path.write_text('---\n' + dump_yaml({'id': table, 'purpose': 'data_description',
                'scope': {'tables': [f'db.public.{table}'], 'columns': columns_in_key}, 'sections': sections})
                + '---\n# Registry\nThis table uses the listed numbering rules.\n'
                + ('# Exception\nThis earlier version is also listed.\n' if 'Exception' in sections else ''), encoding='utf-8')
            paths.append(str(path))
        config = config.model_copy(update={'documents': paths})
    columns = ['code', 'name', 'plant', 'rating', 'supplier']
    tables = [TableSpec(source_id='db', name=table, columns=[ColumnSpec(name=c) for c in columns])
              for table in ('east', 'west')]
    records = {'public.east': [('001', 'Pump', 'P1', 'A', None), ('002', 'Press', 'P1', 'B', None)],
               'public.west': [('001', 'Pump', 'P1', None, 'Acme'), ('002', 'Press XL', 'P1', None, 'Acme')]}

    branches = []
    for table, kind, attribute in [('east', 'InstalledAsset', 'rating'), ('west', 'ServiceAsset', 'supplier')]:
        if same_type:
            kind = 'RegisteredAsset'
        out = tmp_path / table
        scoped = config.model_copy(update={'isolation_domain': table if document_case == 'different_access' else 'plants'})
        if independent and config.documents:
            scoped = scoped.model_copy(update={'documents': [str(tmp_path / f'{table}.md')]})
        with MemorySource(tables, records) as source:
            tools = ConstructionTools(scoped, out / 'construction', adapters=[source])
            try:
                if document_access is not None:
                    peer = 'west' if table == 'east' else 'east'
                    refused = tools.call('read_material', name='document:' + peer, page=0)
                    document_access[table] = {'configured_documents': list(scoped.documents),
                        'visible_document_ids': [d.document_id for d in tools.documents.documents],
                        'peer_read': {k: refused.get(k) for k in ('status', 'code')}}
                headings = {table.title()}
                if (document_case in {'both_cite_registry', 'different_access'}
                        or (document_case == 'one_cites_registry' and table == 'east')):
                    headings.add('Registry')
                if independent and (document_case != 'independent_uncited' or table == 'east'):
                    headings.add('Registry')
                if document_case == 'independent_no_records':
                    probe = tools.call('inspect_source', table='db:public.' + table, operation='get_metadata')
                    refs = [probe['evidence_ref']]
                else:
                    refs = _read_identity_refs(tools, table, headings, table if independent else 'assets')
                draft = _draft(table, 'global', refs[0])
                asset, _ = draft['entities']
                asset.update(type=kind, identity_evidence_refs=refs)
                asset['key_names'] = columns_in_key
                asset['mappings'][0]['key_columns'] = columns_in_key
                if independent:
                    asset['identity_domain'] = f'{table} field directory'
                if same_type and document_case == 'independent_no_records' and table == 'west':
                    asset.update(identity_scope='unknown', identity_domain=None, identity_evidence_refs=[])
                asset['mappings'][0].update(properties={attribute: attribute}, business_context={'status': 'global'})
                relation, = draft['relations']
                relation['source_type'] = kind
                relation['mappings'][0]['source_key_columns'] = columns_in_key
                relation['mappings'][0]['business_context'] = {'status': 'global'}
                _publish(tools, draft)
            finally:
                tools.close()
        branches.append(load_unit_contributor(out, table))
    return project_checkpoint_inputs(branches, 'plant-network')


@pytest.mark.parametrize('document_case', ['both_cite_registry', 'one_cites_registry', 'separate_sections',
    'different_access', 'independent_matched', 'independent_issuer', 'independent_catalog',
    'independent_version', 'independent_roles', 'independent_uncited', 'independent_conflict',
    'independent_no_registry', 'independent_no_records', 'independent_compound', 'independent_compound_swapped'])
def test_cross_table_proposal_uses_only_key_evidence_issued_in_both_extractions(tmp_path, document_case, monkeypatch):
    from lightgraph.construction.overlap_patterns import analyze_overlap_patterns
    from lightgraph.construction.projection_proposals import verify_projection_proposals
    from lightgraph.runtime.config import MergePolicyConfig

    branches = independent_projection_inputs(tmp_path, document_case)
    independent = document_case.startswith('independent_')
    registry_matches = document_case in {'independent_matched', 'independent_compound'}
    baseline = merge_construction_graphs(branches)
    card, = [p for p in analyze_overlap_patterns(branches).patterns if p['kind'] == 'entity'
             and {member['type'] for member in p['members']} == {'InstalledAsset', 'ServiceAsset'}]
    decision = {'action': 'propose', 'pattern_id': card['pattern_id'], 'alignment': [
        {'member_index': i, 'source_index': member['source_indexes'][0],
         'role_index': next(j for j, role in enumerate(member['roles']) if role['end'] == 'source')}
        for i, member in enumerate(card['members'])]}
    if independent:
        import json
        from lightgraph.construction.overlap_review import review_overlap_patterns
        from lightgraph.tests.test_overlap_pattern_model import configured, respond, setup_provider
        def answer(request):
            issued, = json.loads(json.loads(request.content)['messages'][1]['content'])['patterns']
            comparison, = issued['binding_comparisons']['rows']
            if registry_matches:
                assert comparison['versioned_directory'] == 'matches'
            elif document_case in {'independent_catalog', 'independent_issuer', 'independent_version',
                                   'independent_roles', 'independent_compound_swapped'}:
                assert comparison['versioned_directory'] == 'different'
            else:
                assert 'versioned_directory' not in comparison
            assert b'asset-board' not in request.content and b'Pump' not in request.content
            return respond(json.dumps({'decisions': [decision]}))
        setup_provider(monkeypatch, answer)
        reviewed = review_overlap_patterns(configured(), [card], api_key='test')
        assert reviewed['reviewed_patterns'] == 1
        decision, = reviewed['decisions']
    policy, (report,) = verify_projection_proposals(MergePolicyConfig(), branches, [card], [decision])
    assert (report['overlapping_keys'], report['matched_key_names']) == (2, 1)
    assert report['rejected_keys_by_reason']['missing_or_conflicting_names'] == 1
    merged = merge_construction_graphs(branches, merge_policy=policy)
    (receipt,) = report['cross_table_matched_key_checks']
    first_result = ('incompatible_identity_contract' if independent and not registry_matches
                    else 'observations_agree')
    assert receipt == {'keys': 1, 'first_observation_result': first_result,
                       **({'shared_registry_key_verified': True} if registry_matches else {}),
                       'shared_key_section_verified': document_case in {'both_cite_registry', 'different_access'},
                       'unit_fingerprints_match': document_case != 'different_access'}
    if document_case != 'both_cite_registry' and not registry_matches:
        reason = 'unverified_access_fingerprint' if document_case == 'different_access' else 'cross_table_identity_unverified'
        assert report['rejected_keys_by_reason'][reason] == 1
        assert not policy.verified_projection_rules and merged == baseline
        return
    basis = 'shared_versioned_registry' if independent else 'shared_versioned_key_section'
    assert report['verified_groups_by_basis'] == {basis: 1}, report
    assert len(merged['nodes']) == len(baseline['nodes']) - 1
    pump, = [n for n in merged['nodes'] if n['identity'].get('code') == '001']
    if independent:
        locators = {s['provenance']['global_identity_authority']['registry_key_claims_v1'][0]['section']
                    for s in pump['supports']}
        assert len(locators) == 2
    assert pump['properties'] == {'rating': ['A'], 'supplier': ['Acme']}
    assert {(s['contributor_id'], s['reviewed_input_node_id']) for s in pump['supports']} == {
        (b['contributor_id'], n['id']) for b in branches for n in b['graph']['nodes'] if n['identity'].get('code') == '001'}
    assert {n['label'] for n in merged['nodes'] if n['identity'].get('code') == '002'} == {'Press', 'Press XL'}
    assert len(merged['edges']) == len(baseline['edges']) == 4
    input_edges = {(b['contributor_id'], e['id'], e['type'], e['source'], e['target'])
                   for b in branches for e in b['graph']['edges']}
    recovered = set()
    for edge in merged['edges']:
        for support in edge['supports']:
            owner = support['contributor_id']
            recovered.add((owner, edge['id'], edge['type'], edge['source'], edge['target']))
            for original in support.get('endpoint_origins', ()):
                recovered.add((owner, original['edge_id'], original['type'], original['source'], original['target']))
    assert input_edges <= recovered
    assert {(s['contributor_id'], s['provenance']['source']['table']) for edge in merged['edges']
            if edge['source'] == pump['id'] for s in edge['supports']} == {('east', 'east'), ('west', 'west')}


@pytest.mark.parametrize('document_case', ['independent_matched', 'independent_catalog', 'independent_no_records'])
def test_same_type_definition_forks_can_materialize_only_the_proven_rows(tmp_path, document_case):
    from lightgraph.construction.overlap_patterns import analyze_overlap_patterns
    from lightgraph.construction.projection_proposals import verify_projection_proposals
    from lightgraph.runtime.config import MergePolicyConfig

    access = {}
    branches = independent_projection_inputs(tmp_path, document_case, same_type=True, document_access=access)
    for branch in branches:
        owner = branch['contributor_id']
        expected = [] if document_case == 'independent_no_records' else [owner]
        observed_docs = {e['details']['document_id'] for e in branch['evidence_records'].values()
                         if e.get('kind') == 'document_section'}
        assert access[owner]['visible_document_ids'] == expected and observed_docs == set(expected)
        assert access[owner]['peer_read'] == {'status': 'error', 'code': 'unknown_document_page'}
    assert len({b['graph']['access_fingerprint'] for b in branches}) == 1
    baseline = merge_construction_graphs(branches)
    assets = [n for n in baseline['nodes'] if n.get('original_type') == 'RegisteredAsset']
    assert len(assets) == 4 and len({n['type'] for n in assets}) == 2
    assert all(n['type'].startswith('RegisteredAsset__def_') for n in assets)
    card, = [p for p in analyze_overlap_patterns(branches).patterns if p['kind'] == 'entity'
             and {m['type'] for m in p['members']} == {'RegisteredAsset'}]
    proposal = {'action': 'propose', 'pattern_id': card['pattern_id'], 'alignment': [
        {'member_index': i, 'source_index': m['source_indexes'][0], 'role_index': 0}
        for i, m in enumerate(card['members'])]}
    policy, (verified,) = verify_projection_proposals(MergePolicyConfig(), branches, [card], [proposal])
    merged = merge_construction_graphs(branches, merge_policy=policy)
    if document_case != 'independent_matched':
        assert not verified['verified_groups'] and verified['rejected_keys_by_reason'] == {
            'cross_table_identity_unverified': 1, 'missing_or_conflicting_names': 1}
        assert merged == baseline
        return
    assert verified['verified_groups_by_basis'] == {'shared_versioned_registry': 1}
    applied = merged['fusion_diagnostics']['model_verified_projections']['applied_by_pattern'][card['pattern_id']]
    assert applied == {'applied_groups': 1, 'collapsed_nodes': 1}
    assert len(merged['nodes']) == len(baseline['nodes']) - 1
    pump, = [n for n in merged['nodes'] if n['identity'].get('code') == '001']
    assert pump['type'] in {n['type'] for n in assets}
    assert (pump['type'], pump['id']) in {(n['type'], n['id']) for n in assets if n['identity']['code'] == '001'}
    assert pump['properties'] == {'rating': ['A'], 'supplier': ['Acme']}
    assert {(s['contributor_id'], s['reviewed_input_node_id']) for s in pump['supports']} == {
        (b['contributor_id'], n['id']) for b in branches for n in b['graph']['nodes'] if n['identity'].get('code') == '001'}
    assert {n['type'] for n in merged['nodes'] if n['identity'].get('code') == '002'} == {n['type'] for n in assets}
    assert len(merged['edges']) == len(baseline['edges']) == 4
    assert {s['contributor_id'] for e in merged['edges'] if e['source'] == pump['id'] for s in e['supports']} == {'east', 'west'}
