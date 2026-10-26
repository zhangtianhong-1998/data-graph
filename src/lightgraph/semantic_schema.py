"""Bounded project vocabulary; declaring a word never grants source-value access."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import LightGraphError


class Definition(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=1000)

    @field_validator("name")
    @classmethod
    def safe_name(cls, value):
        if not value[0].isalpha() or not all(c.isalnum() or c == "_" for c in value):
            raise ValueError("invalid_semantic_name")
        if value.casefold() in {"rows", "raw_rows", "raw_row", "samples", "sample_rows",
                               "raw_sql", "sql", "prompt", "response", "trace", "evidence_text"}:
            raise ValueError("source_payload_name_forbidden")
        return value


class EntityTypeDefinition(Definition):
    pass


class PropertyDefinition(Definition):
    value_type: Literal["string", "integer", "number", "boolean"] = "string"
    entity_kinds: list[str] = Field(min_length=1, max_length=64)
    multi_valued: bool = False


class RelationDefinition(Definition):
    source_kinds: list[str] = Field(min_length=1, max_length=64)
    target_kinds: list[str] = Field(min_length=1, max_length=64)


class SemanticSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    entity_types: list[EntityTypeDefinition] = Field(default_factory=list, max_length=64)
    properties: list[PropertyDefinition] = Field(default_factory=list, max_length=256)
    relations: list[RelationDefinition] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def unique_names(self):
        for entries in (self.entity_types, self.properties, self.relations):
            if len({entry.name for entry in entries}) != len(entries):
                raise ValueError("duplicate_semantic_definition")
        return self

    @classmethod
    def merged(cls, *schemas: "SemanticSchema") -> "SemanticSchema":
        """Merge compatible contracts; paraphrased descriptions do not change types.

        A different value type, multiplicity or endpoint contract needs an
        explicit schema migration rather than an implicit overwrite.
        """
        entries = {name: {} for name in ("entity_types", "properties", "relations")}
        for schema in schemas:
            schema = cls.model_validate(schema.model_dump() if isinstance(schema, cls) else schema)
            for category, indexed in entries.items():
                for item in getattr(schema, category):
                    previous = indexed.get(item.name)
                    if previous and previous.model_dump(exclude={"description"}) != item.model_dump(exclude={"description"}):
                        raise LightGraphError("semantic_definition_conflict")
                    indexed.setdefault(item.name, item)
        return cls(**{name: [indexed[key] for key in sorted(indexed)] for name, indexed in entries.items()})

    def is_empty(self) -> bool:
        return not (self.entity_types or self.properties or self.relations)
