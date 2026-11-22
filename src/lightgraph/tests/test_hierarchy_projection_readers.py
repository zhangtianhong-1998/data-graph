"""Identity lineage is evidence scoped to a published graph and a visible support."""
import json
from copy import deepcopy

import pytest

from lightgraph.construction.access import filter_graph_scopes
from lightgraph.construction.graph_export import serialize_graph
from lightgraph.construction.graph_storage import read_graph_record, write_graph_record
from lightgraph.errors import LightGraphError
from lightgraph.retrieval import SubgraphRetriever, sync_graph_to_retrieval


def test_new_projection_ids_replace_old_retrieval_endpoints_and_keep_visible_lineage(tmp_path):
    origins = [
        {"id": "old-left", "type": "Item", "identity": {"code": "01"}, "identity_domain": "alpha"},
        {"id": "old-right", "type": "Member", "identity": {"accession": "01"}, "identity_domain": "beta"},
        {"id": "old-private", "type": "Private", "identity": {"code": "01"}, "identity_domain": "secret"},
    ]
    old_nodes = [
        {**origin, "supports": [{"scope_ref": scope, "label": "First", "contributor_id": origin["id"]}]}
        for origin, scope in zip(origins, ["public", "public", "restricted"])
    ]
    old_graph = {"nodes": old_nodes, "edges": [], "support_visibility_version": 1}
    old_snapshot = tmp_path / "before.graph"
    write_graph_record(old_snapshot, old_graph)
    old_generation = sync_graph_to_retrieval(read_graph_record(old_snapshot), tmp_path)["generation"]
    previous = SubgraphRetriever(tmp_path, expected_generation=old_generation)
    old_cohorts = previous.authorized_cohorts(["public"])
    old_ids = [o["id"] for o in origins]
    node = {"id": "canonical", "type": "Item", "identity": {"code": "01"}, "supports": [
        {**deepcopy(n["supports"][0]), "identity_basis": "observed_hierarchy_projection_v1",
         "identity_origins": [origin]} for n, origin in zip(old_nodes, origins)
    ]}
    graph = {**old_graph, "nodes": [node]}
    snapshot = tmp_path / "after.graph"
    write_graph_record(snapshot, graph)
    graph = read_graph_record(snapshot)
    assert read_graph_record(old_snapshot) == old_graph
    visible = filter_graph_scopes(graph, ["public"])
    assert visible["merged_observation_nodes"] == 1
    kg = json.loads(serialize_graph(visible, "semantica-json"))
    assert "secret" not in json.dumps(kg)
    assert kg["entities"][0]["metadata"]["identity_origins"] == origins[:2]
    assert filter_graph_scopes(graph, ["restricted"])["merged_observation_nodes"] == 0

    generation = sync_graph_to_retrieval(graph, tmp_path)["generation"]
    assert generation != old_generation
    current = SubgraphRetriever(tmp_path, expected_generation=generation)
    with pytest.raises(LightGraphError, match="retrieval_index_stale"):
        SubgraphRetriever(tmp_path, expected_generation=old_generation)
    assert set(previous.index.load_authorized_nodes(old_ids, scopes=old_cohorts)) == set(old_ids[:2])
    indexed = current.index.load_authorized_nodes([*old_ids, "canonical"], scopes=current.authorized_cohorts(["public"]))
    assert set(indexed) == {"canonical"}
    observations = indexed["canonical"]["observations"]
    assert all(o["identity_basis"] == "observed_hierarchy_projection_v1" for o in observations)
    assert {o["identity_origins"][0]["id"] for o in observations} == set(old_ids[:2])
    assert "secret" not in json.dumps(observations)
