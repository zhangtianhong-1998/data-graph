"""Evidence-gated correspondence between immutable old and replacement bindings.

This module never merges by name and never rewrites graph history. Replacement
publications still supply every new entity, property, edge and support; the store
validates their complete dependency closure before changing any current head.
"""
from dataclasses import asdict, dataclass
from collections import defaultdict
import json

from ..contracts import canonical
from ..errors import LightGraphError


@dataclass(frozen=True)
class BindingMigration:
    old_publication: str
    new_publication: str
    old_binding: str
    new_binding: str
    old_entity: str
    new_entity: str
    identity_domain: str
    evidence_refs: tuple[str, ...]
    status: str
    conflicts: tuple[str, ...] = ()


def validate_identity_domain_rows(store, rows):
    """Reject conflicting single-valued support in the exact prospective graph."""
    domain_entities = {row["entity_id"] for row in store.connection.execute(
        "SELECT entity_id FROM entity_identity WHERE semantic_namespace LIKE 'identity_domain:%'")}
    values = defaultdict(set)
    multivalued = {"alias", "english_name", "business_code", "source_position", "level", "level_no"}
    for row in rows:
        if row["fact_kind"] != "PropertyValue" or row["status"] != "accepted":
            continue
        payload = json.loads(row["payload"])
        if payload["entity_id"] not in domain_entities or payload["name"] in multivalued:
            continue
        key = (row["isolation_domain"], payload["entity_id"], payload["name"])
        values[key].add(canonical(payload["value"]))
        if len(values[key]) > 1:
            raise LightGraphError("identity_domain_attribute_conflict")


def _decode(value):
    # Legacy registry accepts already-canonical values and stores them as JSON
    # strings; decode that envelope without changing scalar business codes.
    value = json.loads(value)
    return json.loads(value) if isinstance(value, str) else value


def plan_identity_migration(store, publication_ids):
    """Read a candidate map for the exact old heads replaced by this group.

    Compare only identical source/namespace/local-code bindings within the same
    logical rule. The new rule's validated identity domain supplies independent
    sharing evidence; labels are used solely to reject property conflicts.
    """
    plans = []
    for new_id in publication_ids:
        new_pub = store._publication(new_id)
        previous = store.connection.execute(
            "SELECT publication_id FROM partition_head WHERE logical_rule_id=? AND partition_id=?",
            (new_pub["logical_rule_id"], new_pub["partition_id"])).fetchone()
        if not previous or previous[0] == new_id:
            continue
        definition = store.connection.execute(
            "SELECT payload FROM rule WHERE rule_id=? AND revision=?",
            (new_pub["logical_rule_id"], new_pub["revision"])).fetchone()
        if not definition:
            continue
        rule = json.loads(definition[0])
        identity = rule["identity"]
        if identity["semantic_resolution"] != "verified_identity_domain":
            continue
        evidence = tuple(identity.get("identity_evidence", []))
        if not identity.get("identity_domain") or not evidence:
            raise LightGraphError("migration_identity_evidence_missing")

        def bindings(pub):
            result = {}
            publication = store._publication(pub)
            stored_rule = store.connection.execute("SELECT payload FROM rule WHERE rule_id=? AND revision=?",
                (publication["logical_rule_id"], publication["revision"])).fetchone()
            source_rule = json.loads(stored_rule[0]) if stored_rule else {}
            for row in store._fact_rows([pub]):
                if row["status"] != "accepted":
                    continue
                payload = json.loads(row["payload"])
                if row["fact_kind"] not in {"NodeExistence", "PropertyValue"}:
                    continue
                binding = store.connection.execute(
                    "SELECT source_ref,namespace_ref,accepted_business_key FROM source_binding WHERE binding_id=?",
                    (row["binding_id"],)).fetchone()
                if not binding:
                    continue
                namespace = _decode(binding["namespace_ref"])
                key = _decode(binding["accepted_business_key"])
                if not isinstance(namespace, dict) or not isinstance(key, list) or not key:
                    continue
                source_columns = set(namespace.get("key_columns", [])) - set(namespace.get("namespace_columns", []))
                source_identity = source_rule.get("identity", {})
                if source_rule.get("kind") == "wide_hierarchy":
                    # Only a wide slot's key has an optional relative level.
                    # Its real column group is checked separately below.
                    business_key = ["wide_slot", key[-1]]
                    if source_identity.get("level_policy") != "relative_path":
                        levels = source_rule.get("hierarchy", {}).get("levels", [])
                        if source_identity.get("level_policy") == "level_qualified" and len(key) >= 2:
                            source_columns = {level["code"] for level in levels if level["level"] == key[-2]}
                        else:
                            source_columns = {level["code"] for level in levels}
                else:
                    key_columns = source_identity.get("key_columns", [])
                    if len(key_columns) != len(key):
                        continue
                    # A composite entity key must retain every business field.
                    # Match named fields so column order alone cannot cross-bind
                    # (group A, code 1) to (group B, code 1) or to another group.
                    business_key = ["business_key", {
                        column: value for column, value in zip(key_columns, key)
                        if column not in namespace.get("namespace_columns", [])
                    }]
                match = canonical([row["isolation_domain"], binding["source_ref"],
                    namespace.get("namespace_values", []), business_key])
                item = result.setdefault((match, row["binding_id"], payload["entity_id"]),
                                         {"properties": {}, "kind": None, "source_columns": source_columns})
                if row["fact_kind"] == "NodeExistence":
                    item["kind"] = payload["kind"]
                elif payload["name"] not in {"source_position", "level", "level_no", "alias", "business_code"}:
                    item["properties"].setdefault(payload["name"], set()).add(canonical(payload["value"]))
            return result

        old_bindings, new_bindings = bindings(previous[0]), bindings(new_id)
        for (old_key, old_binding, old_entity), old in old_bindings.items():
            matches = [(key, item) for key, item in new_bindings.items()
                       if key[0] == old_key and old["source_columns"] & item["source_columns"]]
            targets = {key[2] for key, _ in matches}
            for (_, new_binding, new_entity), new in matches:
                if old_entity == new_entity:
                    continue
                conflicts = []
                if len(targets) > 1:
                    conflicts.append("ambiguous_identity_migration")
                if old["kind"] and new["kind"] and old["kind"] != new["kind"]:
                    conflicts.append("migration_entity_kind_conflict")
                for name in old["properties"].keys() & new["properties"].keys():
                    if old["properties"][name] != new["properties"][name]:
                        conflicts.append("migration_attribute_conflict")
                plans.append(BindingMigration(previous[0], new_id, old_binding, new_binding,
                    old_entity, new_entity, identity["identity_domain"], evidence,
                    "unresolved" if conflicts else "verified", tuple(sorted(set(conflicts)))))
    return plans


def record_identity_migration(store, plans):
    """Record verified candidates inside the caller's publication transaction.

    Publication state is authoritative: a record whose new publication is still
    staging is a proposal, never evidence that migration has committed.
    """
    if any(plan.status != "verified" for plan in plans):
        raise LightGraphError("identity_migration_conflict")
    store.connection.execute("""CREATE TABLE IF NOT EXISTS identity_migration (
        old_publication TEXT NOT NULL, new_publication TEXT NOT NULL,
        old_binding TEXT NOT NULL, new_binding TEXT NOT NULL,
        old_entity TEXT NOT NULL, new_entity TEXT NOT NULL,
        identity_domain TEXT NOT NULL, evidence_refs TEXT NOT NULL,
        PRIMARY KEY(old_publication,new_publication,old_binding,new_binding)
    )""")
    for plan in plans:
        store.connection.execute("INSERT OR IGNORE INTO identity_migration VALUES(?,?,?,?,?,?,?,?)",
            (plan.old_publication, plan.new_publication, plan.old_binding, plan.new_binding,
             plan.old_entity, plan.new_entity, plan.identity_domain, canonical(plan.evidence_refs)))
    return [asdict(plan) for plan in plans]
