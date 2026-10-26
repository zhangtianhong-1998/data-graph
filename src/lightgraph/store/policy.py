"""Reject untyped and unbounded payloads before SQLite receives a value."""
from __future__ import annotations

import math
import re
import copy
from typing import Any

from lightgraph.contracts import GraphFact, RuleSpec, ValidationRecord, canonical
from lightgraph.errors import LightGraphError
from lightgraph.semantic_schema import SemanticSchema


class PersistenceViolation(LightGraphError):
    pass


KINDS = {
    "business": {"Metric", "Measure", "Dimension", "DimensionMember", "ReportItem", "AnalysisMethod", "AnalysisQuestion", "BusinessConcept"},
    "metadata": {"Database", "Schema", "Table", "Column", "ColumnGroup", "SemanticSlot", "Workbook", "Worksheet", "Constraint"},
    "provenance": {"Document", "DocumentSection", "Rule", "GraphFact"},
}
RELATION_STATES = {
    "has_schema": {"accepted"}, "has_table": {"accepted"}, "table_has_column": {"accepted"},
    "member_of_group": {"candidate", "accepted"}, "expresses_role": {"accepted"},
    "declared_fk": {"accepted"}, "technical_link": {"unresolved"}, "value_overlap": {"candidate"},
    "same_attribute_role": {"candidate", "accepted"}, "verified_reference": {"accepted"},
    "maps_to_concept": {"candidate", "accepted"}, "same_entity": {"candidate", "accepted"},
    "parent_of": {"accepted"}, "member_of": {"accepted"}, "uses_measure": {"accepted"},
    "analyzes": {"candidate", "accepted"}, "defined_by": {"accepted"},
    "documented_by": {"candidate", "accepted"}, "supported_by_rule": {"accepted"},
    "has_sheet": {"accepted"}, "sheet_has_column": {"accepted"}, "has_section": {"accepted"},
    "has_constraint": {"accepted"}, "constraint_column": {"accepted"}, "constraint_target": {"accepted"},
}
BUSINESS_KINDS = KINDS["business"]
RELATION_ENDPOINT_KINDS = {
    "has_schema": ({"Database"}, {"Schema"}),
    "has_table": ({"Schema"}, {"Table"}),
    "table_has_column": ({"Table"}, {"Column"}),
    "member_of_group": ({"Column"}, {"ColumnGroup"}),
    "expresses_role": ({"Column", "ColumnGroup"}, {"SemanticSlot"}),
    "declared_fk": ({"Column"}, {"Column"}),
    "technical_link": ({"Column"}, {"Column"}),
    "value_overlap": ({"Column"}, {"Column"}),
    "same_attribute_role": ({"Column"}, {"Column"}),
    "verified_reference": ({"Column"}, {"Column"}),
    "maps_to_concept": ({"Column", "ColumnGroup"}, {"BusinessConcept"}),
    "same_entity": (BUSINESS_KINDS, BUSINESS_KINDS),
    "parent_of": (BUSINESS_KINDS, BUSINESS_KINDS),
    "member_of": ({"DimensionMember"}, {"Dimension"}),
    "uses_measure": ({"Metric"}, {"Measure"}),
    "analyzes": ({"AnalysisMethod"}, BUSINESS_KINDS),
    "defined_by": (BUSINESS_KINDS, {"DocumentSection", "Rule"}),
    "documented_by": ({"Table", "Column"}, {"DocumentSection"}),
    "supported_by_rule": ({"GraphFact"}, {"Rule"}),
    "has_sheet": ({"Workbook"}, {"Worksheet"}),
    "sheet_has_column": ({"Worksheet"}, {"Column"}),
    "has_section": ({"Document"}, {"DocumentSection"}),
    "has_constraint": ({"Table", "Worksheet"}, {"Constraint"}),
    "constraint_column": ({"Constraint"}, {"Column"}),
    "constraint_target": ({"Constraint"}, {"Column"}),
}
PROPERTY_NAMES = frozenset({
    "label", "alias", "english_name", "business_code", "description", "definition", "unit",
    "formula", "period", "caliber", "granularity", "level", "level_no", "dimension", "taxonomy",
    "source_ref", "schema", "table", "column", "data_type", "nullable", "primary_key", "comment",
    "role", "technical_status", "coverage", "target_kind", "modality", "frequency", "domain",
    "normalization", "semantic_namespace", "aggregation", "document_ref", "section_ref",
    "column_position", "qualified_name", "name", "kind", "scope", "rule_ref", "revision", "source_position",
})
QUALIFIER_NAMES = frozenset({
    "taxonomy", "valid_period", "period", "caliber", "granularity", "unit", "level", "source_level",
    "target_level", "relation_group", "namespace", "condition", "role", "technical_status",
    "coverage", "modality", "semantic_basis", "normalization", "allow_dag", "formula", "document_ref",
    "source_group", "semantic_level", "hierarchy_version", "column_position",
})
OPERATOR_ARGUMENTS = {
    "distinct_projection": {"tuple", "columns", "cap", "limit", "predicate"},
    "emit_entity": {"from", "kind"},
    "project_attributes": {"from", "single_valued", "multi_valued"},
    "emit_alias": {"from", "alias"},
    "emit_relation": {"from", "source", "target", "relation", "qualifiers"},
    "unpivot_levels": {"from", "levels", "taxonomy"},
    "compress_paths": {"from", "gap_policy", "fill_semantics_confirmed"},
    "validate_nodes": {"from", "kind"},
    "reconcile_supports": {"from", "partition_ref"},
    "canary_check": {"from"},
}


class PersistencePolicy:
    """One semantic value per property fact; no row, result-set or evidence blobs."""
    max_fact_bytes = 32768
    max_value_characters = 8192

    def __init__(self):
        self.semantic_schema = SemanticSchema()
        self.kinds = {layer: set(kinds) for layer, kinds in KINDS.items()}
        self.property_definitions = {}
        self.relation_definitions = {}

    def with_schema(self, schema: SemanticSchema):
        """Return a validated copy; rejected declarations cannot widen this policy."""
        schema = SemanticSchema.model_validate(schema.model_dump())
        merged = SemanticSchema.merged(self.semantic_schema, schema)
        result = copy.deepcopy(self)
        reserved = KINDS["metadata"] | KINDS["provenance"]
        if any(item.name in reserved for item in merged.entity_types):
            raise PersistenceViolation("semantic_type_conflicts_with_system_kind")
        result.kinds["business"].update(item.name for item in merged.entity_types)
        for item in merged.properties:
            if not set(item.entity_kinds) <= result.kinds["business"]:
                raise PersistenceViolation("property_kind_not_registered")
            if item.name in PROPERTY_NAMES:
                # Template properties have stable contracts across existing
                # publications. Neither a narrowing nor a silent override is
                # safe here; a new business property needs its own name.
                raise PersistenceViolation("builtin_property_definition_forbidden")
            result.property_definitions[item.name] = item
        for item in merged.relations:
            if not set(item.source_kinds + item.target_kinds) <= result.kinds["business"]:
                raise PersistenceViolation("relation_kind_not_registered")
            if item.name in RELATION_STATES:
                raise PersistenceViolation("builtin_relation_definition_forbidden")
            result.relation_definitions[item.name] = item
        result.semantic_schema = merged
        return result

    def relation_states(self, name: str) -> set[str]:
        if name in self.relation_definitions or name in {"parent_of", "member_of", "uses_measure"}:
            return {"accepted", "candidate", "unresolved"}
        return RELATION_STATES.get(name, set())

    def relation_endpoint_kinds(self, name: str):
        definition = self.relation_definitions.get(name)
        if definition:
            return set(definition.source_kinds), set(definition.target_kinds)
        kinds = self.kinds["business"]
        if name in {"parent_of", "same_entity"}:
            return kinds, kinds
        if name == "analyzes":
            return {"AnalysisMethod"}, kinds
        if name == "defined_by":
            return kinds, {"DocumentSection", "Rule"}
        return RELATION_ENDPOINT_KINDS.get(name)

    def check_property_kind(self, name: str, kind: str) -> None:
        definition = self.property_definitions.get(name)
        if definition and kind not in definition.entity_kinds:
            raise PersistenceViolation("property_entity_kind_mismatch")

    def schema_for_facts(self, facts) -> SemanticSchema:
        """Export only vocabulary used by the authorized projection, not hidden types."""
        kinds = {fact["payload"]["kind"] for fact in facts if fact["fact_kind"] == "NodeExistence"}
        properties = {fact["payload"]["name"] for fact in facts if fact["fact_kind"] == "PropertyValue"}
        relations = {fact["payload"]["relation"] for fact in facts if fact["fact_kind"] == "Edge"}
        return SemanticSchema(
            entity_types=[entry for entry in self.semantic_schema.entity_types if entry.name in kinds],
            properties=[entry.model_copy(update={"entity_kinds": sorted(set(entry.entity_kinds) & kinds)})
                        for entry in self.semantic_schema.properties if entry.name in properties and set(entry.entity_kinds) & kinds],
            relations=[entry.model_copy(update={"source_kinds": sorted(set(entry.source_kinds) & kinds),
                                                "target_kinds": sorted(set(entry.target_kinds) & kinds)})
                       for entry in self.semantic_schema.relations if entry.name in relations
                       and set(entry.source_kinds) & kinds and set(entry.target_kinds) & kinds])

    def check_rule(self, rule: RuleSpec, goal=None) -> None:
        """Check vocabulary and source-column restrictions before any source value is staged."""
        if rule.identity.entity_kind not in self.kinds["business"]:
            raise PersistenceViolation("unregistered_node_kind")
        attributes = set(rule.attributes.single_valued) | set(rule.attributes.multi_valued)
        if attributes - PROPERTY_NAMES - set(self.property_definitions):
            raise PersistenceViolation("property_not_in_output_contract")
        for name in attributes:
            self.check_property_kind(name, rule.identity.entity_kind)
            definition = self.property_definitions.get(name)
            if definition and name in rule.attributes.multi_valued and not definition.multi_valued:
                raise PersistenceViolation("property_multiplicity_mismatch")
        relation = rule.relation.relation if rule.relation else "parent_of" if rule.hierarchy else None
        if relation and not self.relation_states(relation):
            raise PersistenceViolation("unregistered_relation")
        from ..rules.engine import RuleEngine
        columns = RuleEngine.columns(None, rule)
        if set(columns) & {item.column for item in rule.attributes.excluded}:
            raise PersistenceViolation("excluded_column_projected")
        if goal is not None:
            from ..discovery.goal import source_in_goal, column_is_excluded, column_is_evidence_only
            if not source_in_goal(goal, rule.source):
                raise PersistenceViolation("outside_source_goal")
            if goal.entity_kinds and rule.identity.entity_kind not in goal.entity_kinds:
                raise PersistenceViolation("outside_extraction_goal")
            if relation and goal.relation_kinds and relation not in goal.relation_kinds:
                raise PersistenceViolation("outside_relation_goal")
            if any(column_is_excluded(goal, rule.source, column) for column in columns):
                raise PersistenceViolation("excluded_column_projected")
            if any(column_is_evidence_only(goal, rule.source, column) for column in columns):
                raise PersistenceViolation("evidence_only_column_projected")
            def predicate_columns(value):
                if not isinstance(value, dict):
                    return []
                if "not" in value:
                    return predicate_columns(value["not"])
                if any(key in value for key in ("all", "any", "and")):
                    return [column for key in ("all", "any", "and") for part in value.get(key, [])
                            for column in predicate_columns(part)]
                return [value["column"]] if "column" in value else list(value)
            if any(column_is_excluded(goal, rule.source, column) for column in predicate_columns(rule.scope.predicate)):
                raise PersistenceViolation("excluded_column_queried")

    def check_fact(self, fact: GraphFact) -> None:
        if not isinstance(fact, GraphFact):
            raise PersistenceViolation("typed_graph_fact_required")
        payload = fact.payload
        if len(canonical(payload).encode("utf-8")) > self.max_fact_bytes:
            raise PersistenceViolation("graph_fact_too_large")
        if fact.fact_kind == "NodeExistence":
            self._keys(payload, {"entity_id", "kind", "layer"})
            if payload["layer"] not in self.kinds or payload["kind"] not in self.kinds[payload["layer"]]:
                raise PersistenceViolation("unregistered_node_kind")
            self._reference(payload["entity_id"])
        elif fact.fact_kind == "PropertyValue":
            self._keys(payload, {"entity_id", "name", "value"}, {"qualifiers"})
            self._reference(payload["entity_id"])
            if payload["name"] not in PROPERTY_NAMES and payload["name"] not in self.property_definitions:
                raise PersistenceViolation("property_not_in_output_contract")
            self._scalar(payload["value"])
            definition = self.property_definitions.get(payload["name"])
            if definition and payload["value"] is not None:
                types = {"string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,)}
                if type(payload["value"]) not in types[definition.value_type]:
                    raise PersistenceViolation("semantic_property_value_type_mismatch")
            if payload["name"] in {"label", "alias", "english_name"} and not isinstance(payload["value"], str):
                raise PersistenceViolation("name_property_requires_string")
            self._qualifiers(payload.get("qualifiers", {}))
        elif fact.fact_kind == "Edge":
            self._keys(payload, {"source", "relation", "target"}, {"qualifiers"})
            self._reference(payload["source"])
            self._reference(payload["target"])
            if not self.relation_states(payload["relation"]):
                raise PersistenceViolation("unregistered_relation")
            if payload["relation"] == "parent_of" and payload["source"] == payload["target"]:
                raise PersistenceViolation("hierarchy_self_loop")
            self._qualifiers(payload.get("qualifiers", {}))
        else:
            self._keys(payload, {"id", "subject", "predicate", "object", "modality", "conditions"})
            for field in ("id", "subject", "predicate"):
                self._reference(payload[field])
            self._scalar(payload["object"])
            if payload["modality"] not in {"asserted", "possible", "recommended", "conditional", "negated"}:
                raise PersistenceViolation("claim_modality_invalid")
            conditions = payload["conditions"]
            allowed_condition_keys = QUALIFIER_NAMES | {str(i) for i in range(16)}
            if not isinstance(conditions, dict) or len(conditions) > 16 or set(conditions) - allowed_condition_keys:
                raise PersistenceViolation("claim_conditions_invalid")
            for condition in conditions.values():
                if not isinstance(condition, str) or len(condition) > 1024:
                    raise PersistenceViolation("claim_condition_requires_bounded_text")

    def check_record(self, value: object) -> None:
        if isinstance(value, GraphFact):
            self.check_fact(value)
        elif isinstance(value, (RuleSpec, ValidationRecord)):
            if len(canonical(value).encode("utf-8")) > 262144:
                raise PersistenceViolation("record_too_large")
            if isinstance(value, RuleSpec):
                for operator in value.operators:
                    if operator.op not in OPERATOR_ARGUMENTS or set(operator.args) - OPERATOR_ARGUMENTS[operator.op]:
                        raise PersistenceViolation("operator_payload_not_in_output_contract")
                self._reject_raw_fields(value.model_dump(mode="json", by_alias=True))
            elif any(not re.fullmatch(r"[a-z][a-z0-9_]{0,100}", code) for code in value.codes):
                raise PersistenceViolation("validation_error_code_invalid")
        else:
            raise PersistenceViolation("untyped_persistence_forbidden")

    @staticmethod
    def _keys(value: dict, required: set, optional: set | None = None) -> None:
        if not required <= set(value) or set(value) - required - (optional or set()):
            raise PersistenceViolation("payload_fields_not_in_output_contract")

    @staticmethod
    def _reject_raw_fields(value: Any) -> None:
        if isinstance(value, dict):
            if set(value) & {"rows", "raw_rows", "raw_row", "samples", "sample_rows", "raw_sql", "sql", "prompt", "response", "trace", "evidence_text"}:
                raise PersistenceViolation("source_payload_persistence_forbidden")
            for item in value.values():
                PersistencePolicy._reject_raw_fields(item)
        elif isinstance(value, list):
            for item in value:
                PersistencePolicy._reject_raw_fields(item)

    def _scalar(self, value: Any) -> None:
        if type(value) not in (str, int, float, bool, type(None)):
            raise PersistenceViolation("only_scalar_graph_property_allowed")
        if isinstance(value, str) and len(value) > self.max_value_characters:
            raise PersistenceViolation("property_too_large")
        if isinstance(value, float) and not math.isfinite(value):
            raise PersistenceViolation("non_finite_property")

    def _reference(self, value: Any) -> None:
        if not isinstance(value, str) or not value or len(value) > 1024:
            raise PersistenceViolation("invalid_logical_reference")

    def _qualifiers(self, value: Any) -> None:
        if not isinstance(value, dict) or set(value) - QUALIFIER_NAMES:
            raise PersistenceViolation("qualifier_not_in_output_contract")
        for item in value.values():
            self._scalar(item)
