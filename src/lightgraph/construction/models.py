"""Generic, Agent-authored entity and relationship mappings for graph construction."""
from __future__ import annotations

from typing import Any, Literal
from pydantic import Field, field_validator, model_validator
from lightgraph.contracts import StrictModel, canonical


class SourceMapping(StrictModel):
    source_id: str = Field(min_length=1)
    table: str = Field(min_length=1)
    schema_name: str = Field(default="public", alias="schema")
    predicate: dict[str, Any] | None = Field(default=None,
        description='Typed Predicate/1.0 filter: is_not_null/is_null have no value, ne uses value; logical all/any/not (and alias). Read reference:predicate-guide.md. No raw SQL.')
    properties: dict[str, str] = Field(default_factory=dict, max_length=128)
    constant_properties: dict[str, str | int | float | bool] = Field(default_factory=dict, max_length=32,
        description='Agent-declared mapping annotations, such as the observed slot or endpoint positions. Not identity keys or source-verified values.')
    context_columns: list[str] = Field(default_factory=list, max_length=32,
        description='Authorized columns that identify a source-row context shared across mappings. Retained on each support, never added to entity identity. Use the same column set for related mappings.')

    @model_validator(mode='after')
    def unambiguous_property_sources(self):
        if set(self.properties) & set(self.constant_properties):
            raise ValueError('property_source_conflict')
        if len(set(self.context_columns)) != len(self.context_columns):
            raise ValueError('duplicate_context_column')
        return self


class EntityMapping(SourceMapping):
    kind: Literal['row'] = 'row'
    key_columns: list[str] = Field(min_length=1, max_length=32)
    label_column: str | None = Field(default=None,
        description='Physical column containing the primary source name. Choose a confirmed name column when available; omission generates a display/identity fallback and does not check name consistency. Never omit a known name to bypass a conflict.')
    alias_columns: list[str] = Field(default_factory=list, max_length=32,
        description='Confirmed alternate names, translations or abbreviations for the same object. Display columns alone do not become aliases.')
    display_columns: dict[str, str] = Field(default_factory=dict, max_length=32,
        description='Supplementary display components, or a composite display for objects without an independent name. Does not replace label_column or alias_columns and never changes identity.')

    @field_validator('display_columns')
    @classmethod
    def nonempty_display_columns(cls, value):
        if any(not name.strip() or not column.strip() for name, column in value.items()):
            raise ValueError('invalid_display_column')
        return value


COLUMN_IDENTITY_NAMES = ['source_id', 'schema', 'table', 'column']


class ColumnReference(StrictModel):
    source_id: str = Field(min_length=1)
    table: str = Field(min_length=1)
    schema_name: str = Field(default='public', alias='schema')
    column: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1, max_length=128,
        description='Issued metadata or applicable document references supporting this column meaning; never raw values or invented evidence IDs.')


class MetadataColumnMapping(ColumnReference):
    """One authorized catalog column as a physical semantic object, not its values."""
    kind: Literal['metadata_column']
    label: str | None = Field(default=None, min_length=1, max_length=4000,
        description='Agent-declared primary meaning supported by evidence. If omitted, catalog comment then physical column name is used; no fact values are read.')
    aliases: list[str] = Field(default_factory=list, max_length=32)
    semantic_annotations: dict[str, str | int | float | bool] = Field(default_factory=dict, max_length=32,
        description='Evidence-backed Agent interpretations; recorded as declared annotations, separately from observed catalog metadata.')


class ColumnEndpoint(ColumnReference):
    """A fixed column object attached only to observed rows of its own table."""


class EntityDefinition(StrictModel):
    type: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=4000)
    key_names: list[str] = Field(min_length=1, max_length=32)
    mappings: list[EntityMapping | MetadataColumnMapping] = Field(min_length=1, max_length=128)
    display_mode: Literal['auto', 'identity', 'relations'] = Field(default='auto',
        description='For objects without a source label: derive from visible relations or the complete identity. Explicit source labels always take precedence.')

    @model_validator(mode="after")
    def consistent_key_arity(self):
        if len(set(self.key_names)) != len(self.key_names):
            raise ValueError("duplicate_identity_key_name")
        metadata = [isinstance(m, MetadataColumnMapping) for m in self.mappings]
        if any(metadata):
            if not all(metadata):
                raise ValueError('mixed_entity_identity_origins')
            if self.key_names != COLUMN_IDENTITY_NAMES:
                raise ValueError('metadata_column_identity_required')
            return self
        if any(len(m.key_columns) != len(self.key_names) for m in self.mappings):
            raise ValueError("entity_key_arity_mismatch")
        return self


class EndpointLookup(StrictModel):
    """Resolve one endpoint through actual equal-key matches in an authorized table."""
    source_id: str = Field(min_length=1)
    table: str = Field(min_length=1)
    schema_name: str = Field(default='public', alias='schema')
    predicate: dict[str, Any] | None = Field(default=None,
        description='Typed Predicate/1.0 filter: is_not_null/is_null have no value, ne uses value; logical all/any/not (and alias). Read reference:predicate-guide.md. No raw SQL.')
    match_columns: list[str] = Field(min_length=1, max_length=32,
        description='Columns matched to the corresponding relation key_columns, in order. Include tenant or other required matching scope explicitly.')
    key_columns: list[str] = Field(min_length=1, max_length=32,
        description='Columns from the matched row that form the resolved entity identity.')
    cardinality: Literal['one', 'many'] = Field(default='one',
        description='one rejects matches to multiple distinct endpoint identities; many explicitly permits all observed matches for this input row.')
    on_missing: Literal['error', 'omit'] = Field(default='error',
        description='error diagnoses unmatched references; omit explicitly omits unmatched input rows and reports their count.')

    @model_validator(mode='after')
    def unique_columns(self):
        if any(len(columns) != len(set(columns)) for columns in (self.match_columns, self.key_columns)):
            raise ValueError('duplicate_lookup_column')
        return self


class RelationMapping(SourceMapping):
    source_key_columns: list[str] = Field(default_factory=list, max_length=32)
    target_key_columns: list[str] = Field(default_factory=list, max_length=32)
    source_metadata: ColumnEndpoint | None = None
    target_metadata: ColumnEndpoint | None = None
    metadata_basis: Literal['schema_applies_to_rows', 'observed_non_null'] | None = Field(default=None,
        description='Required for a column endpoint. schema_applies_to_rows declares applicability to actual selected rows using evidence; observed_non_null restricts rows by that physical column IS NOT NULL without retaining its values. NULL absence never proves non-applicability.')
    source_lookup: EndpointLookup | None = None
    target_lookup: EndpointLookup | None = None
    equal_endpoints: Literal["error", "omit", "include"] = "error"

    @model_validator(mode='after')
    def matching_arity(self):
        endpoints = [getattr(self, end + '_metadata') for end in ('source', 'target')]
        if sum(value is not None for value in endpoints) > 1:
            raise ValueError('metadata_relation_requires_row_endpoint')
        if any(endpoints) != (self.metadata_basis is not None):
            raise ValueError('metadata_relation_basis_required')
        for end in ('source', 'target'):
            metadata = getattr(self, end + '_metadata')
            lookup = getattr(self, end + '_lookup')
            keys = getattr(self, end + '_key_columns')
            if metadata is not None:
                if lookup is not None or keys:
                    raise ValueError('ambiguous_relation_endpoint')
                if (metadata.source_id, metadata.schema_name, metadata.table) != (self.source_id, self.schema_name, self.table):
                    raise ValueError('metadata_relation_input_table_mismatch')
            elif not keys:
                raise ValueError('row_endpoint_key_required')
            if lookup and len(getattr(self, end + '_key_columns')) != len(lookup.match_columns):
                raise ValueError('lookup_match_arity_mismatch')
        return self


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
    unresolved: list[str] = Field(default_factory=list, max_length=128,
        description="Only unmet requirements of the current user goal. Empty when covered; never invent missing work.")
    decisions: list[str] = Field(default_factory=list, max_length=128,
        description="Evidence-backed choices already applied; these do not prevent completion.")
    suggestions: list[str] = Field(default_factory=list, max_length=128,
        description="Optional future improvements outside the current goal; these do not prevent completion.")

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
                for end in ('source', 'target'):
                    kind = getattr(relation, end + '_type')
                    metadata = getattr(mapping, end + '_metadata')
                    entity = next(item for item in self.entities if item.type == kind)
                    metadata_entity = isinstance(entity.mappings[0], MetadataColumnMapping)
                    if (metadata is not None) != metadata_entity:
                        raise ValueError('relation_endpoint_origin_mismatch')
                    if metadata is not None:
                        if not any((m.source_id, m.schema_name, m.table, m.column) ==
                                   (metadata.source_id, metadata.schema_name, metadata.table, metadata.column)
                                   for m in entity.mappings):
                            raise ValueError('metadata_endpoint_mapping_missing')
                    else:
                        lookup = getattr(mapping, end + '_lookup')
                        keys = lookup.key_columns if lookup else getattr(mapping, end + '_key_columns')
                        if len(keys) != kinds[kind]:
                            raise ValueError('relation_key_arity_mismatch')
                if mapping.equal_endpoints == "include" and not relation.allow_self_links:
                    raise ValueError("self_link_permission_required")
        try:
            canonical(self.model_dump(mode="python", by_alias=True))
        except (TypeError, ValueError):
            raise ValueError("unsupported_graph_draft_value") from None
        return self


def iter_source_mappings(draft):
    """Enumerate every physical dependency, including relation lookup tables."""
    for definition in draft.entities + draft.relations:
        for mapping in definition.mappings:
            yield mapping
            for name in ('source_lookup', 'target_lookup', 'source_metadata', 'target_metadata'):
                lookup = getattr(mapping, name, None)
                if lookup is not None:
                    yield lookup


def iter_fact_mappings(draft):
    """Dependencies needing fact access; column objects need only the catalog."""
    for definition in draft.entities + draft.relations:
        for mapping in definition.mappings:
            if isinstance(mapping, MetadataColumnMapping):
                continue
            yield mapping
            for name in ('source_lookup', 'target_lookup'):
                lookup = getattr(mapping, name, None)
                if lookup is not None:
                    yield lookup


class EvidenceRecord(StrictModel):
    ref: str
    kind: str
    context_fingerprint: str
    source_ref: dict[str, Any] | str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class DataValidation(StrictModel):
    validation_id: str | None = None
    graph_path: str | None = None
    read_window: dict[str, Any] = Field(default_factory=dict)
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
    last_successful_validation: DataValidation | None = None


def draft_evidence_refs(draft: GraphDraft) -> set[str]:
    return set(draft.evidence_refs).union(*(set(getattr(mapping, 'evidence_refs', []))
        for mapping in iter_source_mappings(draft)))
