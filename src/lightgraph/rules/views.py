"""Rooted hierarchy views are pure projections of an already authorized graph."""
from collections import deque

from ..contracts import StrictModel, Scalar
from ..errors import LightGraphError
from pydantic import Field, model_validator


class HierarchyView(StrictModel):
    taxonomy: str = Field(min_length=1)
    root_entity: str = Field(min_length=1)
    max_depth: int = Field(default=32, ge=0, le=4096)
    context_qualifiers: dict[str, Scalar] = Field(default_factory=dict)
    @model_validator(mode="after")
    def validate_context(self):
        if set(self.context_qualifiers) - {"condition", "valid_period", "period", "hierarchy_version"}:
            raise ValueError("invalid_hierarchy_view_context")
        if any(value is None or value == "" for value in self.context_qualifiers.values()):
            raise ValueError("empty_hierarchy_view_context")
        return self


def rooted_view(store, scopes, definition: HierarchyView, *, domain="default", publication_id=None):
    """Read only: the root's display depth is zero and parent facts remain intact."""
    definition = HierarchyView.model_validate(definition)
    graph = store.view(scopes, domain=domain, publication_id=publication_id)
    node_map = {node["id"]: node for node in graph["nodes"]}
    if definition.root_entity not in node_map:
        raise LightGraphError("view_root_not_visible")
    context = {"taxonomy": definition.taxonomy, **definition.context_qualifiers}
    matching = [edge for edge in graph["edges"] if edge["relation"] == "parent_of"
        and {key: value for key, value in edge.get("qualifiers", {}).items()
             if key in {"taxonomy", "condition", "valid_period", "period", "hierarchy_version"}} == context]
    adjacency = {}
    for edge in matching:
        adjacency.setdefault(edge["source"], []).append(edge["target"])
    depths = {definition.root_entity: 0}
    queue = deque([definition.root_entity])
    while queue:
        node = queue.popleft()
        if depths[node] >= definition.max_depth:
            continue
        for child in adjacency.get(node, []):
            if child not in depths:
                depths[child] = depths[node] + 1
                queue.append(child)
    memberships = []
    for fact in graph["facts"]:
        payload = fact["payload"]
        if fact["fact_kind"] != "PropertyValue" or payload["name"] != "source_position":
            continue
        qualifiers = payload.get("qualifiers", {})
        membership_context = {key: value for key, value in qualifiers.items()
            if key in {"taxonomy", "condition", "valid_period", "period", "hierarchy_version"}}
        if payload["entity_id"] in depths and membership_context == context:
            memberships.append({"entity_id": payload["entity_id"], "source_position": payload["value"],
                **qualifiers, "fact_id": fact["id"], "support_ids": fact["support_ids"]})
    return {"definition": definition.model_dump(), "historical": graph["historical"],
        "nodes": [{**node_map[node], "display_depth": depth}
                  for node, depth in sorted(depths.items(), key=lambda item: (item[1], item[0]))],
        "edges": [edge for edge in matching if edge["source"] in depths and edge["target"] in depths],
        "memberships": memberships,
        "publications": graph["publications"]}
