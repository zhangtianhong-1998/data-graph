"""A pinned inheritance includes the human identity constraints of its source."""
from pathlib import Path

import pytest

from lightgraph.construction.inheritance import (
    freeze_inherited_graphs, frozen_contributors, load_inherited_source_graph,
)
from lightgraph.contracts import digest
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import InheritedGraphRef
from lightgraph.tests.test_merge_candidate_evidence import candidate_unit
from lightgraph.tests.test_web_graph_review import client
from lightgraph.tests.test_web_publication_identity import publish, listing, submit, revoke


def test_revoked_cannot_link_changes_pin_without_rewriting_frozen_graph(client):
    publish(client, [candidate_unit(side, [(side, ['code'])]) for side in ('one', 'two')])
    current = listing(client)
    decision = submit(client, current, current['candidates'][0], verdict='different')
    assert decision.status_code == 200
    ref = InheritedGraphRef(inherit_id='source', source_project_id='p', allowed_scopes=['public'])
    workspace = client.app.state.workspace
    original, fingerprint = load_inherited_source_graph(ref, workspace=workspace)
    assert len(original['identity_separations']) == 1
    ref.expected_graph_digest = fingerprint
    assert load_inherited_source_graph(ref, workspace=workspace)[1] == fingerprint
    workspace.create('Derived', 'derived')
    config = workspace.result_config('derived')
    config.inherited_graphs = [ref]
    root = Path(config.output_dir) / 'construction'
    freeze_inherited_graphs(config, root, workspace=workspace)
    assert revoke(client, listing(client), decision.json()).status_code == 200
    with pytest.raises(LightGraphError, match='inherited_graph_digest_mismatch'):
        load_inherited_source_graph(ref, workspace=workspace)
    frozen, _ = frozen_contributors(root, config=config)
    assert len(frozen[0]['graph']['identity_separations']) == 1


def test_legacy_topology_digest_cannot_pin_identity_constraints(client):
    publish(client, [candidate_unit(side, [(side, ['code'])]) for side in ('one', 'two')])
    ref = InheritedGraphRef(inherit_id='source', source_project_id='p')
    graph, _ = load_inherited_source_graph(ref, workspace=client.app.state.workspace)
    ref.expected_graph_digest = digest({key: graph[key] for key in ('nodes', 'edges', 'definitions')})
    with pytest.raises(LightGraphError, match='inherited_graph_digest_mismatch'):
        load_inherited_source_graph(ref, workspace=client.app.state.workspace)
