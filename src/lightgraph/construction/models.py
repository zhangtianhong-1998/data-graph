"""Generic, Agent-authored entity and relationship mappings for graph construction."""
from __future__ import annotations

from typing import Any, Literal
from pydantic import Field, field_validator, model_validator
from lightgraph.contracts import StrictModel, canonical


class SourceMapping(StrictModel):
    source_id: str = Field(min_length=1)
    table: str = Field(min_length=1)
    schema_name: str = Field(default="public", alias="schema")
    predicate: dict[str, Any] | None = None
    properties: dict[str, str] = Field(default_factory=dict, max_length=128)


class EntityMapping(SourceMapping):
    key_columns: list[str] = Field(min_length=1, max_length=32)
    label_column: str | None = None
    alias_columns: list[str] = Field(default_factory=list, max_length=32)


class EntityDefinition(StrictModel):
    type: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=4000)
    key_names: list[str] = Field(min_length=1, max_length=32)
    mappings: list[EntityMapping] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def consistent_key_arity(self):
        if len(set(self.key_names)) != len(self.key_names):
            raise ValueError("duplicate_identity_key_name")
        if any(len(m.key_columns) != len(self.key_names) for m in self.mappings):
            raise ValueError("entity_key_arity_mismatch")
        return self


class RelationMapping(SourceMapping):
    source_key_columns: list[str] = Field(min_length=1, max_length=32)
    target_key_columns: list[str] = Field(min_length=1, max_length=32)
    equal_endpoints: Literal["error", "omit", "include"] = "error"


class RelationDefinition(StrictModel):
    type: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=4000)
    source_type: str = Field(min_length=1, max_length=100)
    target_type: str = Field(min_length=1, max_length=100)
    allow_self_links: bool = False
    acyclic: bool = False
    mappings: list[RelationMapping] = Field(min_length=1, max_length=128)


class GraphDraft(StrictModel):
    draft_id: str = Field(min_length=1, max_length=80)
    purpose: str = Field(default="", max_length=4000)
    entities: list[EntityDefinition] = Field(default_factory=list, max_length=128)
    relations: list[RelationDefinition] = Field(default_factory=list, max_length=128)
    evidence_refs: list[str] = Field(default_factory=list, max_length=512)
    rationale: str = Field(default="", max_length=8000)
    unresolved: list[str] = Field(default_factory=list, max_length=128)

    @field_validator("draft_id")
    @classmethod
    def readable_identifier(cls, value: str) -> str:
        if not value.strip() or not all(c.isalnum() or c in "_-" for c in value):
            raise ValueError("invalid_graph_draft_id")
        return value

    @model_validator(mode="after")
    def coherent_definitions(self):
        for definitions in (self.entities, self.relations):
            if len({d.type for d in definitions}) != len(definitions):
                raise ValueError("duplicate_graph_type")
        kinds = {e.type: len(e.key_names) for e in self.entities}
        for relation in self.relations:
            if relation.source_type not in kinds or relation.target_type not in kinds:
                raise ValueError("unknown_relation_entity_type")
            for mapping in relation.mappings:
                if (len(mapping.source_key_columns) != kinds[relation.source_type]
                        or len(mapping.target_key_columns) != kinds[relation.target_type]):
                    raise ValueError("relation_key_arity_mismatch")
                if mapping.equal_endpoints == "include" and not relation.allow_self_links:
                    raise ValueError("self_link_permission_required")
        try:
            canonical(self.model_dump(mode="python", by_alias=True))
        except (TypeError, ValueError):
            raise ValueError("unsupported_graph_draft_value") from None
        return self


class EvidenceRecord(StrictModel):
    ref: str
    kind: str
    context_fingerprint: str
    source_ref: dict[str, Any] | str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class DataValidation(StrictModel):
    revision: int = Field(ge=1)
    draft_fingerprint: str
    context_fingerprint: str
    passed: bool
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    graph_digest: str | None = None


class DraftRecord(StrictModel):
    draft: GraphDraft
    revision: int = Field(ge=1)
    fingerprint: str
    context_fingerprint: str
    status: Literal["candidate", "validated", "needs_revision", "stale", "published"] = "candidate"
    validation: DataValidation | None = None


def draft_evidence_refs(draft: GraphDraft) -> set[str]:
    return set(draft.evidence_refs)
