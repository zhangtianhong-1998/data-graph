"""Offer metadata publication only when its existing authorization gate permits it."""
import yaml
import pytest

from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import dump_yaml
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import reconnect
from lightgraph.tests.test_construction_qualification_recovery import star_source, star_draft


@pytest.mark.parametrize('readonly_failure', [False, True], ids=['catalog_still_qualified', 'readonly_rejected'])
def test_metadata_publication_advice_matches_executable_gate(tmp_path, monkeypatch, readonly_failure):
    monkeypatch.setattr(reconnect, '_recovery_pause', lambda source: None)
    source, connections, data = star_source(4, readonly_failure=readonly_failure)
    tools = ConstructionTools(RunConfig(sources=[], model={'mode': 'in_environment'}),
                              tmp_path / 'work', adapters=[source])
    try:
        ref = tools.call('inspect_source', table='facts:public.items', operation='get_metadata')['evidence_ref']
        assert tools.call('save_graph_draft', yaml_text=dump_yaml(star_draft(ref)))['status'] == 'ok'
        check = tools.call('validate_graph_draft', draft_id='star')
        assert not check['passed']
        attempts = len(connections)
        advice = check['metadata_definitions']
        workspace = yaml.safe_load(tools.call('read_workspace')['content'])
        assert workspace['metadata_definition_options'] == [advice]
        derived = tools.call('save_graph_draft', metadata_from_draft='star')
        if readonly_failure:
            assert advice['available'] is False
            assert advice['reason'] == 'metadata_catalog_requalification_required'
            assert 'tool' not in advice and 'arguments' not in advice
            assert advice['alternative']['arguments'] == {'draft_id': 'star', 'mode': 'structure'}
            assert derived['code'] == advice['reason']
            assert derived['failure_class'] == 'source_qualification'
            assert 'Changing the request' in derived['next_action']
            assert 'not a verified instance graph' in derived['next_action']
            assert tools.call('validate_graph_draft', draft_id='star', mode='structure')['passed']
            assert tools.call('publish_graph_draft', draft_id='star', mode='structure')['status'] == 'ok'
            assert not tools.workspace.read_published_graph()['nodes']
        else:
            assert advice['available'] is True and derived['status'] == 'ok'
            child = derived['draft_id']
            assert tools.call('validate_graph_draft', draft_id=child)['passed']
            assert tools.call('publish_graph_draft', draft_id=child)['nodes'] == 2
        assert len(connections) == attempts  # Every alternative used saved declarations only.
    finally:
        tools.close()
        source.close()
        data.close()
