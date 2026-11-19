import json
from copy import deepcopy
from pathlib import Path
from xml.etree import ElementTree as ET

from fastapi.testclient import TestClient

from lightgraph.cli import main
from lightgraph.construction.access import access_fingerprint, filter_graph_scopes
from lightgraph.construction.graph_export import serialize_graph
from lightgraph.construction.merger import write_sharded_graph
from lightgraph.construction.snapshot_registry import register_access_context
from lightgraph.tests.test_business_context_retrieval import _graph
from lightgraph.tests.test_web_graph_history import publish
from lightgraph.web.app import create_app
from lightgraph.web.workspace import Workspace


def test_json_and_graphml_preserve_parallel_edge_identity_and_witnesses():
    graph = _graph()
    duplicate = deepcopy(graph['edges'][0])
    duplicate.update(id='观察 2', type='另一种关系')
    graph['edges'].append(duplicate)
    selected = filter_graph_scopes(graph, ['public'], business_context={'project': 'M'})
    kg = json.loads(serialize_graph(selected, 'semantica-json'))
    assert set(kg) == {'entities', 'relationships', 'metadata'}
    assert kg['metadata']['business_context'] == {'project': 'M'}
    entities = {node['id']: node for node in kg['entities']}
    assert set(entities) == {'m', 'common', 'global2'}
    assert entities['common']['properties']['observed'] == ['common', 'm_fact']
    edges = {edge['id']: edge for edge in kg['relationships']}
    assert edges['观察 2']['source'] == edges['m_common']['source'] == 'm'
    assert edges['观察 2']['target'] == edges['m_common']['target'] == 'common'
    assert edges['观察 2']['metadata']['supports'][0]['provenance']['evidence_refs']
    assert 'K_SECRET' not in str(kg)
    xml = ET.fromstring(serialize_graph(selected, 'graphml'))
    ns = {'g': 'http://graphml.graphdrawing.org/xmlns'}
    assert xml.find('g:graph', ns).get('edgedefault') == 'directed'

    def field(item, key):
        return item.findtext(f'g:data[@key="{key}"]', namespaces=ns)

    assert {n.get('id') for n in xml.findall('.//g:node', ns)} == set(entities)
    for e in xml.findall('.//g:edge', ns):
        original = field(e, 'original_id')
        edge = edges.pop(json.loads(original) if original else e.get('id'))
        assert (e.get('source'), e.get('target')) == (edge['source'], edge['target'])
        assert field(e, 'type') == edge['type']
        assert json.loads(field(e, 'properties')) == edge['properties']
        assert json.loads(field(e, 'metadata')) == edge['metadata']
    assert not edges


def test_export_api_pins_version_and_applies_project_and_access_scopes(tmp_path):
    workspace = Workspace(tmp_path / 'workspaces')
    workspace.create('Graph export', 'p')
    config = workspace.result_config('p')
    old = publish(config, 'old')
    root = Path(config.output_dir) / 'construction'
    current = _graph()
    current.update(access_fingerprint=access_fingerprint(config), status='published', target='graph')
    write_sharded_graph(root, current, context_fingerprint='current', result_policy_id=register_access_context(root, config))
    with TestClient(create_app(workspace.root, frontend_dir=tmp_path / 'absent')) as client:
        path = '/api/projects/p/graph/export'
        scoped = client.get(path, params={'business_context': '{"project":"M"}'})
        assert scoped.status_code == 200 and 'attachment;' in scoped.headers['content-disposition']
        assert {n['id'] for n in scoped.json()['entities']} == {'m', 'common', 'global2'}
        assert 'K_SECRET' not in scoped.text
        history = client.get(path, params={'format': 'graphml', 'version': old})
        assert history.status_code == 200 and 'application/graphml+xml' in history.headers['content-type']
        assert history.content.count(b'<node ') == 1 and b'PRIVATE' not in history.content
        assert old in history.headers['content-disposition']
        assert client.get(path, params={'version':'../graphml'}).status_code == 404
        assert client.get(path, params={'format':'invalid'}).status_code in {400, 422}
        assert client.get(path, params={'business_context':'invalid'}).status_code == 400
        output = tmp_path / 'export.json'
        assert main(['export-graph', str(workspace.project_path('p') / 'config.yaml'), str(output),
                     '--format', 'semantica-json', '--business-context', '{"project":"M"}']) == 0
        assert json.loads(output.read_text()) == scoped.json()
        current_config = workspace.get_config('p', offline=True)
        current_config['scopes'] = []
        workspace._write_config('p', current_config)
        assert client.get(path, params={'version':old}).json()['entities'] == []
