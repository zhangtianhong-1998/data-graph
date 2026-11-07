"""A successful legacy snapshot is not proof under the repaired key semantics."""
from lightgraph.contracts import digest
from lightgraph.construction.access import access_fingerprint
from lightgraph.construction.intent import effective_goal_markdown
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import dump_yaml
from lightgraph.tests.test_construction_tools import config_for, draft


def test_legacy_context_requires_revalidation_without_deleting_history(tmp_path):
    config = config_for(tmp_path)
    workspace = tmp_path / 'construction'
    tools = ConstructionTools(config, workspace)
    try:
        # This is the exact d96e911 context shape: no source-key version.
        legacy_context = digest(
            config.goal.model_dump(mode='json', by_alias=True), effective_goal_markdown(config),
            config.isolation_domain, access_fingerprint(config),
            [table.model_dump(mode='json', by_alias=True) for _, table in sorted(tools.tables.items())],
            tools._source_hashes, [source.model_dump(mode='json') for source in config.sources],
            None, [])
        assert tools.workspace.context_fingerprint != legacy_context
        tools.workspace.context_fingerprint = legacy_context
        proposal = draft(tools.describe_sources()['tables'][0])
        name = proposal['draft_id']
        assert tools.call('save_graph_draft', yaml_text=dump_yaml(proposal))['status'] == 'ok'
        assert tools.call('validate_graph_draft', draft_id=name)['passed']
        assert tools.call('publish_graph_draft', draft_id=name)['status'] == 'ok'
        historical_files = {path: path.read_bytes() for path in (workspace / 'published').glob('*.yaml')}
    finally:
        tools.close()

    current = ConstructionTools(config, workspace)
    try:
        assert current.workspace.get_draft(name).status == 'stale'
        rejected = current.call('publish_graph_draft', draft_id=name)
        assert rejected['status'] == 'error'
        assert all(path.read_bytes() == content for path, content in historical_files.items())
        saved = current.call('save_graph_draft', yaml_text=dump_yaml(proposal))
        assert saved['status'] == 'ok' and saved['revision'] == 2
        assert current.call('validate_graph_draft', draft_id=name)['passed']
        assert current.call('publish_graph_draft', draft_id=name)['status'] == 'ok'
        assert len(current.workspace.read_published_graph()['nodes']) == 2
    finally:
        current.close()
