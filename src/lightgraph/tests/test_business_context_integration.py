"""Fixed-source extraction, publication, history and retrieval share one context contract."""
from copy import deepcopy
import json
import re

from fastapi.testclient import TestClient

from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.construction.access import access_fingerprint
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workflow import load_unit_contributor, publish_premerged_graph
from lightgraph.construction.workspace import dump_yaml
from lightgraph.sources import MemorySource
from lightgraph.web.app import create_app


def _extract_branch(config, directory, project):
    """Run actual save/validate/publish tools over A+C or B+C source fixtures."""
    source_id = 'A' if project == 'M' else 'B'
    facts = TableSpec(source_id=source_id, name='facts_' + project, columns=[ColumnSpec(name=name) for name in
        ['project', 'period', 'code', 'product', 'amount', 'name', 'product_name', 'legacy_name']])
    catalog = TableSpec(source_id='C', name='catalog', columns=[ColumnSpec(name=name) for name in ['code', 'name']])
    links = TableSpec(source_id='C', name='catalog_links', columns=[ColumnSpec(name=name) for name in ['code', 'parent']])
    rows = {facts.qualified_name: [(project, 'Q1' if project == 'M' else 'Q2', 'SAME', 'P001',
        100 if project == 'M' else 900, project + '项目场景', project + '项目产品名', '未知场景')],
        catalog.qualified_name: [('P001', '共享产品'), ('P002', '通用根')],
        links.qualified_name: [('P001', 'P002')]}
    scoped = {'status': 'scoped', 'columns': {'project': 'project', 'period': 'period'}}
    global_context = {'status': 'global'}
    fact_mapping = {'source_id': source_id, 'table': facts.name,
                    'context_columns': ['project', 'period', 'code', 'product']}
    scenario_keys = ['code'] if project == 'M' else ['project', 'code']
    directory.mkdir(parents=True, exist_ok=True)
    registry = directory / 'products.md'
    registry.write_text(
        '---\nid: product-registry\npurpose: data_description\n'
        'scope: {tables: [A.public.facts_M, B.public.facts_K, C.public.catalog], columns: [product, code]}\n'
        '---\n# Product keys\nThe product column in both fact tables stores the globally unique code '
        'from the shared C catalog. All three columns use the C_product_catalog registry.\n',
        encoding='utf-8')
    config = config.model_copy(update={'documents': [*config.documents, str(registry)]})
    with MemorySource([facts, catalog, links], rows) as adapter:
        tools = ConstructionTools(config, directory / 'construction', adapters=[adapter], drop_unbound_documents=True)
        try:
            source = next(t for t in tools.describe_sources()['tables'] if t['name'] == 'catalog')
            evidence = tools.call('inspect_source', table=source['table'], operation='get_metadata')
            assert evidence['status'] == 'ok', evidence
            document = tools.call('read_material', name='document:product-registry')
            assert document['section_fully_read'], document
            draft = {'draft_id': 'branch_' + project, 'entities': [
                {'type': 'Product', 'key_names': ['code'], 'identity_scope': 'global',
                 'identity_domain': 'C_product_catalog',
                 'identity_evidence_refs': [evidence['evidence_ref'], *document['evidence_refs']],
                 'mappings': [
                     {'source_id': 'C', 'table': 'catalog', 'key_columns': ['code'], 'label_column': 'name',
                      'business_context': global_context},
                     {**fact_mapping, 'key_columns': ['product'], 'label_column': 'product_name',
                      'properties': {'amount': 'amount'}, 'business_context': scoped}]},
                {'type': 'Scenario', 'key_names': scenario_keys,
                 **({'identity_scope': 'scoped', 'identity_context_keys': ['project']} if project == 'K' else {}),
                 'mappings': [{**fact_mapping, 'key_columns': scenario_keys, 'label_column': 'name',
                               'business_context': scoped}]},
            ], 'relations': [
                {'type': 'catalog_parent', 'source_type': 'Product', 'target_type': 'Product',
                 'mappings': [{'source_id': 'C', 'table': 'catalog_links', 'source_key_columns': ['code'],
                               'target_key_columns': ['parent'], 'business_context': global_context}]},
                {'type': 'uses', 'source_type': 'Scenario', 'target_type': 'Product',
                 'mappings': [{**fact_mapping, 'source_key_columns': scenario_keys, 'target_key_columns': ['product'],
                               'properties': {'amount': 'amount'}, 'business_context': scoped}]},
            ]}
            if project == 'M':
                draft['entities'].append({'type': 'LegacyUnknown', 'key_names': ['code'], 'mappings': [
                    {**fact_mapping, 'key_columns': ['code'], 'label_column': 'legacy_name'}]})
            saved = tools.call('save_graph_draft', yaml_text=dump_yaml(draft))
            assert saved['status'] == 'ok', saved
            validated = tools.call('validate_graph_draft', draft_id=draft['draft_id'])
            assert validated['passed'], validated
            published = tools.call('publish_graph_draft', draft_id=draft['draft_id'])
            assert published['status'] == 'ok', published
        finally:
            tools.close()
    return load_unit_contributor(directory, 'extract_' + project)


def _publish(config, graph):
    return publish_premerged_graph(config.output_dir, config, merged=graph,
        included_units=['extract_M', 'extract_K'], skipped_units=[], unit_attempts={})


def _get(client, url, **params):
    response = client.get(url, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_two_branch_publication_context_canvas_history_details_batches_and_retrieval(tmp_path):
    app = create_app(tmp_path / 'workspace', frontend_dir=tmp_path / 'absent')
    with TestClient(app) as client:
        client.post('/api/projects', json={'project_id': 'p', 'name': 'Context integration'}).raise_for_status()
        workspace = app.state.workspace
        stored = workspace.get_config('p')
        workspace.save_config('p', {**stored, 'embedding': {'provider': 'local_hash'}})
        config = workspace.resolved_config('p')
        config.model.mode = 'in_environment'
        original = [_extract_branch(config, tmp_path / 'branch_M', 'M'),
                    _extract_branch(config, tmp_path / 'branch_K', 'K')]
        merged = merge_construction_graphs(original, target_access_fingerprint=access_fingerprint(config))
        # The incompatible Scenario grain remains two definitions; healthy
        # catalog identity merges without any user-defined fusion policy.
        assert len(merged['nodes']) == 5
        assert len([n for n in merged['nodes'] if n['type'] == 'Product']) == 2
        assert merged['definition_conflicts']
        assert all(row['status'] == 'unresolved' for row in merged['definition_conflicts'])
        summary = _publish(config, merged)
        assert summary['nodes'] == 5 and summary['edges'] == 3
        base = '/api/projects/p/graph'
        full = _get(client, base, include_candidates='true')
        assert len(full['nodes']) == 5 and full['definition_conflicts']
        version = full['version_id']
        choices = _get(client, base + '/contexts')
        assert {'project': 'M'} in choices['contexts'] and {'project': 'K'} in choices['contexts']
        assert choices['support_counts']['unknown'] == 1 and choices['support_counts']['global'] > 0
        selected = {'business_context': json.dumps({'project': 'M'})}
        m = _get(client, base, **selected)
        k = _get(client, base, business_context=json.dumps({'project': 'K'}))
        assert len(m['nodes']) == len(k['nodes']) == 3
        assert len(m['edges']) == len(k['edges']) == 2
        assert 'K项目' not in str(m) and '未知场景' not in str(m)
        assert 'M项目' not in str(k) and '未知场景' not in str(k)
        assert len(_get(client, base, business_context='{}')['nodes']) == 2
        product = next(n for n in merged['nodes'] if n['type'] == 'Product' and n['identity']['code'] == 'P001')
        k_scenario = next(n for n in merged['nodes'] if n.get('original_type') == 'Scenario' and n['identity'].get('project') == 'K')
        details = _get(client, base + '/details', entity_id=product['id'], **selected)
        assert 'K项目' not in str(details) and not re.search(r'\b900\b', json.dumps(details, ensure_ascii=False))
        assert any(f['payload'].get('name') == 'amount' and f['payload'].get('value') == 100 for f in details['facts'])
        assert client.get(base + '/details', params={'entity_id': k_scenario['id'], **selected}).status_code == 404
        first = _get(client, base + '/batches', limit=1, **selected)
        transported = []
        offset = 0
        while offset is not None:
            page = _get(client, base + '/batches', limit=1, offset=offset, generation=first['generation'], **selected)
            transported.extend(page['nodes'])
            offset = page['next_offset']
        assert {n['id'] for n in transported} == {n['id'] for n in m['nodes']}
        cross = client.get(base + '/batches', params={'generation': first['generation'], 'business_context': json.dumps({'project': 'K'})})
        assert cross.status_code == 400 and cross.json()['error']['code'] == 'graph_generation_changed', cross.text
        edge_page = _get(client, base + '/batches', part='edges', generation=first['generation'], **selected)
        assert len(edge_page['edges']) == 2 and 'K项目' not in str(edge_page)

        synced = client.post('/api/projects/p/retrieval/sync')
        assert synced.status_code == 200, synced.text
        query = client.post('/api/projects/p/retrieval/query', json={
            'query': 'M项目场景', 'business_context': {'project': 'M'}, 'top_p_seeds': 1, 'hops': 2})
        assert query.status_code == 200, query.text
        result = query.json()
        assert {n['id'] for n in result['nodes']} == {n['id'] for n in m['nodes']}
        assert 'K项目' not in str(result) and '未知场景' not in str(result) and not re.search(r'\b900\b', json.dumps(result, ensure_ascii=False))
        assert result['business_context'] == {'project': 'M'}
        assert all('observations' in edge for edge in result['edges'])
        retrieved_product = next(node for node in result['nodes'] if node['id'] == product['id'])
        catalog_witness = next(obs for obs in retrieved_product['observations']
            if obs['provenance']['source']['source_id'] == 'C')
        assert {origin['contributor_id'] for origin in catalog_witness['execution_origins']} == {'extract_M', 'extract_K'}
        assert any(obs.get('row_context', {}).get('values', {}).get('project') == 'M'
                   for obs in retrieved_product['observations'])
        invalid = client.post('/api/projects/p/retrieval/query', json={
            'query': 'M项目场景', 'business_context': {'project': []}})
        assert invalid.status_code == 400 and invalid.json()['error']['code'] == 'invalid_business_context_selection'

        # Replace the head, then exercise every pinned history surface with the
        # original context. History never silently reads the latest graph.
        replacement = deepcopy(merged)
        replacement['nodes'] = [deepcopy(product)]
        replacement['nodes'][0]['supports'] = [support for support in replacement['nodes'][0]['supports']
            if support['business_context']['status'] == 'global'
            or support['business_context']['dimensions'].get('project') == 'M']
        replacement['edges'] = []
        replacement['definition_conflicts'] = []
        _publish(config, replacement)
        assert len(_get(client, base, **selected)['nodes']) == 1
        assert {'project': 'K'} not in _get(client, base + '/contexts')['contexts']
        historical = _get(client, base, version=version, **selected)
        assert {n['id'] for n in historical['nodes']} == {n['id'] for n in m['nodes']}
        historic_details = _get(client, base + '/details', version=version, entity_id=product['id'], **selected)
        assert not re.search(r'\b900\b', json.dumps(historic_details, ensure_ascii=False)) and 'K项目' not in str(historic_details)
        historic_batch = _get(client, base + '/batches', version=version, **selected)
        assert len(historic_batch['nodes']) == 3
        assert 'K项目' not in str(historic_batch)
        assert {'project': 'K'} in _get(client, base + '/contexts', version=version)['contexts']
        # Cached history options also honor a later withdrawal of read grants.
        workspace.save_config('p', {**workspace.get_config('p'), 'scopes': []})
        hidden = _get(client, base + '/contexts', version=version)
        assert hidden['contexts'] == [] and hidden['total_contexts'] == 0
        assert hidden['support_counts'] == {}
        assert _get(client, base, version=version, **selected)['nodes'] == []
