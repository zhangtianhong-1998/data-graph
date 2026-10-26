"""SQLite stores accepted graph projections, never source rows or model traces.

Publication heads are the sole current-view authority. Every replacement is a
complete, consistent partition and advances its generation atomically. Source
watermarks advance only when every declared partition in a window is published.
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from typing import Any, Iterable

from lightgraph.contracts import GraphFact, PartitionCompletion, RuleSpec, SupportSpec, ValidationRecord, canonical, digest
from lightgraph.identity import IdentityRegistry
from lightgraph.errors import LightGraphError
from .policy import PersistencePolicy, PersistenceViolation, KINDS
from lightgraph.semantic_schema import SemanticSchema, EntityTypeDefinition


class StoreError(LightGraphError):
    pass


class PublicationConflict(StoreError):
    pass


class IncompletePartition(StoreError):
    pass


class GraphStore:
    def __init__(self, path: str | Path = ":memory:", policy: PersistencePolicy | None = None):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.policy = policy or PersistencePolicy()
        self._lock = threading.RLock()
        self._initialize()
        self._goal = None
        self._goal_revision = 0
        for row in self.connection.execute("SELECT payload FROM semantic_catalog ORDER BY catalog_id"):
            self.policy = self.policy.with_schema(SemanticSchema.model_validate_json(row[0]))
        self.identity = IdentityRegistry(self.connection)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "GraphStore":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    @contextmanager
    def _transaction(self):
        with self._lock:
            # Batch operations such as snapshot import own an outer atomic
            # transaction; individual store writes must not commit it early.
            savepoint = "store_" + uuid.uuid4().hex if self.connection.in_transaction else None
            self.connection.execute("SAVEPOINT " + savepoint if savepoint else "BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                if savepoint:
                    self.connection.execute("ROLLBACK TO " + savepoint)
                    self.connection.execute("RELEASE " + savepoint)
                else:
                    self.connection.execute("ROLLBACK")
                raise
            else:
                self.connection.execute("RELEASE " + savepoint if savepoint else "COMMIT")

    @contextmanager
    def import_transaction(self):
        """Import vocabulary and graph together; a rejected snapshot leaves neither."""
        with self._lock:
            prior_policy = self.policy
            try:
                with self._transaction():
                    if self.heads():
                        raise StoreError("import_requires_empty_store")
                    yield
            except BaseException:
                self.policy = prior_policy
                raise

    def _initialize(self) -> None:
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS graph_fact (
          fact_id TEXT PRIMARY KEY, isolation_domain TEXT NOT NULL, fact_kind TEXT NOT NULL, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fact_support (
          support_id TEXT PRIMARY KEY, fact_id TEXT NOT NULL REFERENCES graph_fact(fact_id),
          origin TEXT NOT NULL, binding_id TEXT NOT NULL, scope_ref TEXT NOT NULL,
          partition_id TEXT NOT NULL, rule_id TEXT NOT NULL, revision INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS publication (
          publication_id TEXT PRIMARY KEY, logical_rule_id TEXT NOT NULL, revision INTEGER NOT NULL,
          partition_id TEXT NOT NULL, source_versions TEXT NOT NULL, base_generation INTEGER NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('staging','ready','published','aborted')),
          checkpoint_ref TEXT, published_at TEXT
        );
        CREATE TABLE IF NOT EXISTS publication_support (
          publication_id TEXT NOT NULL REFERENCES publication(publication_id),
          support_id TEXT NOT NULL REFERENCES fact_support(support_id),
          validation_ref TEXT, document_ref TEXT, binding_revision INTEGER NOT NULL, status TEXT NOT NULL,
          PRIMARY KEY(publication_id,support_id)
        );
        CREATE TABLE IF NOT EXISTS publication_binding (
          publication_id TEXT NOT NULL REFERENCES publication(publication_id), binding_id TEXT NOT NULL,
          binding_revision INTEGER NOT NULL, PRIMARY KEY(publication_id,binding_id,binding_revision)
        );
        CREATE TABLE IF NOT EXISTS rule_generation (
          publication_id TEXT PRIMARY KEY REFERENCES publication(publication_id),
          base_heads TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS semantic_catalog (
          catalog_id INTEGER PRIMARY KEY AUTOINCREMENT,
          goal_id TEXT NOT NULL, goal_revision INTEGER NOT NULL,
          rule_id TEXT NOT NULL, rule_revision INTEGER NOT NULL, payload TEXT NOT NULL,
          UNIQUE(goal_id,goal_revision,rule_id,rule_revision)
        );
        CREATE TABLE IF NOT EXISTS partition_head (
          logical_rule_id TEXT NOT NULL, partition_id TEXT NOT NULL,
          publication_id TEXT NOT NULL REFERENCES publication(publication_id), generation INTEGER NOT NULL,
          checkpoint_ref TEXT, valid INTEGER NOT NULL DEFAULT 1,
          PRIMARY KEY(logical_rule_id,partition_id)
        );
        CREATE TABLE IF NOT EXISTS rule (
          rule_id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
          lifecycle TEXT NOT NULL, dependency_state TEXT NOT NULL DEFAULT 'valid',
          PRIMARY KEY(rule_id,revision)
        );
        CREATE TABLE IF NOT EXISTS validation_record (
          validation_id TEXT PRIMARY KEY, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS source_window (
          window_id TEXT PRIMARY KEY, source_ref TEXT NOT NULL, checkpoint_ref TEXT NOT NULL,
          enumeration_complete INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending'
        );
        CREATE TABLE IF NOT EXISTS window_partition (
          window_id TEXT NOT NULL REFERENCES source_window(window_id), rule_id TEXT NOT NULL,
          partition_id TEXT NOT NULL, publication_id TEXT REFERENCES publication(publication_id),
          PRIMARY KEY(window_id,rule_id,partition_id)
        );
        CREATE TABLE IF NOT EXISTS source_watermark (
          source_ref TEXT PRIMARY KEY, window_id TEXT NOT NULL, checkpoint_ref TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rule_event (
          event_id TEXT PRIMARY KEY, rule_id TEXT NOT NULL, revision INTEGER NOT NULL,
          event_type TEXT NOT NULL, reason_code TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS document_claim_cache (
          document_rule_id TEXT PRIMARY KEY,
          publication_id TEXT NOT NULL REFERENCES publication(publication_id),
          payload TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS fact_support_fact ON fact_support(fact_id);
        CREATE INDEX IF NOT EXISTS publication_by_rule ON publication(logical_rule_id,partition_id);
        """)

    @staticmethod
    def _check_schema_goal(schema, goal):
        if goal is None:
            return
        kinds = {entry.name for entry in schema.entity_types}
        kinds.update(kind for entry in schema.properties for kind in entry.entity_kinds)
        kinds.update(kind for entry in schema.relations for kind in entry.source_kinds + entry.target_kinds)
        if goal.entity_kinds and kinds - set(goal.entity_kinds):
            raise PersistenceViolation("semantic_schema_outside_entity_goal")
        if goal.relation_kinds and {entry.name for entry in schema.relations} - set(goal.relation_kinds):
            raise PersistenceViolation("semantic_schema_outside_relation_goal")

    def configure_goal(self, goal, goal_revision: int = 1) -> None:
        """Install user intent; vocabulary declarations cannot grant source permissions."""
        from ..contracts import ExtractionGoal
        goal = ExtractionGoal.model_validate(goal.model_dump())
        if not isinstance(goal_revision, int) or goal_revision < 1:
            raise StoreError("invalid_goal_revision")
        declared_names = {entry.name for entry in goal.semantic_schema.entity_types}
        declarations = SemanticSchema(entity_types=[EntityTypeDefinition(name=name,
            description=f"User-declared output type: {name}") for name in sorted(set(goal.entity_kinds) - KINDS["business"] - declared_names)])
        schema = SemanticSchema.merged(goal.semantic_schema, declarations)
        self._check_schema_goal(schema, goal)
        policy = self.policy.with_schema(schema)
        self._persist_semantic_schema(schema, goal.goal_id, goal_revision, "", 0)
        self._goal, self._goal_revision, self.policy = goal, goal_revision, policy

    def _rule_schema(self, rule) -> SemanticSchema:
        schema = SemanticSchema.merged(self._goal.semantic_schema if self._goal else SemanticSchema(),
                                       rule.semantic_schema)
        kind = rule.identity.entity_kind
        # An explicitly named output kind is operator authorization. Automatic
        # mode must supply its new definition instead of admitting any string.
        if (self._goal and kind in self._goal.entity_kinds and
                kind not in KINDS["business"] and
                kind not in {entry.name for entry in schema.entity_types}):
            schema = SemanticSchema.merged(schema, SemanticSchema(entity_types=[
                EntityTypeDefinition(name=kind, description=f"User-declared output type: {kind}")]))
        return schema

    def preview_rule_semantics(self, rule):
        """Validate a proposed vocabulary and mapping without persisting a declaration."""
        schema = self._rule_schema(rule)
        self._check_schema_goal(schema, self._goal)
        policy = self.policy.with_schema(schema)
        policy.check_record(rule)
        policy.check_rule(rule, self._goal)
        return policy

    def register_rule_semantics(self, rule) -> None:
        policy = self.preview_rule_semantics(rule)
        goal_id = self._goal.goal_id if self._goal else "explicit_rule"
        self._persist_semantic_schema(self._rule_schema(rule), goal_id, self._goal_revision,
                                      rule.rule_id, rule.revision)
        self.policy = policy

    def _persist_semantic_schema(self, schema, goal_id, goal_revision, rule_id, rule_revision):
        if schema.is_empty():
            return
        payload = canonical(schema)
        if len(payload.encode("utf-8")) > 262144:
            raise PersistenceViolation("semantic_schema_too_large")
        with self._transaction():
            old = self.connection.execute("""SELECT payload FROM semantic_catalog
                WHERE goal_id=? AND goal_revision=? AND rule_id=? AND rule_revision=?""",
                (goal_id, goal_revision, rule_id, rule_revision)).fetchone()
            if old and SemanticSchema.model_validate_json(old[0]) != schema:
                raise StoreError("semantic_schema_revision_is_immutable")
            self.connection.execute("""INSERT OR IGNORE INTO semantic_catalog
                (goal_id,goal_revision,rule_id,rule_revision,payload) VALUES(?,?,?,?,?)""",
                (goal_id, goal_revision, rule_id, rule_revision, payload))

    def register_import_schema(self, schema: SemanticSchema) -> None:
        """An imported result is explicit user input, still subject to typed contracts."""
        policy = self.policy.with_schema(schema)
        self._persist_semantic_schema(schema, "imported_snapshot", 1, "", 0)
        self.policy = policy

    def get_document_cache(self, document_rule_id: str):
        """Reuse only projections associated with the current valid publication."""
        from lightgraph.documents import ClaimCache
        row = self.connection.execute("""
          SELECT c.payload FROM document_claim_cache c JOIN partition_head h
          ON h.logical_rule_id=c.document_rule_id AND h.partition_id='all'
          AND h.publication_id=c.publication_id AND h.valid=1
          WHERE c.document_rule_id=?
        """, (document_rule_id,)).fetchone()
        if not row:
            return None
        try:
            return ClaimCache.model_validate_json(row[0])
        except Exception:
            raise StoreError('document_claim_cache_invalid') from None

    def put_document_cache(self, publication_id: str, cache) -> None:
        """Persist typed candidate claims after publication; source text is forbidden."""
        from lightgraph.documents import ClaimCache
        if not isinstance(cache, ClaimCache):
            raise PersistenceViolation('typed_claim_cache_required')
        # Revalidate nested mutation and forbid any unregistered output fields.
        try:
            cache = ClaimCache.model_validate(cache.model_dump())
        except Exception:
            raise PersistenceViolation('document_claim_cache_invalid') from None
        payload = cache.model_dump_json()
        if len(payload.encode('utf-8')) > 8 * 1024 * 1024:
            raise PersistenceViolation('document_claim_cache_too_large')
        self.policy._reject_raw_fields(cache.model_dump())
        with self._transaction():
            pub = self._publication(publication_id)
            rid = pub['logical_rule_id']
            head = self.connection.execute(
                "SELECT publication_id,valid FROM partition_head WHERE logical_rule_id=? AND partition_id='all'",
                (rid,)).fetchone()
            if not rid.startswith('document:') or pub['state'] != 'published' or not head or head[0] != publication_id or not head[1]:
                raise StoreError('document_cache_requires_current_publication')
            if any('document:' + e.document_id != rid for e in cache.entries.values()):
                raise PersistenceViolation('document_claim_cache_scope_mismatch')
            self.connection.execute(
                'INSERT OR REPLACE INTO document_claim_cache VALUES(?,?,?)', (rid, publication_id, payload))

    def begin_publication(self, rule_id: str, revision: int, partition_id: str,
                          source_versions: dict[str, str | list[str]], *, base_generation: int | None = None,
                          replace_rule: bool = False) -> str:
        """Stage one partition, or a complete rule generation without source-key tokens.

        Full generations compare the old publication identities at commit time.
        Query partition values and ordering are deliberately absent from that CAS.
        """
        for ref in (rule_id, partition_id):
            self.policy._reference(ref)
        if replace_rule and partition_id != "all":
            raise StoreError("rule_generation_requires_all_partition")
        if revision < 1 or not isinstance(source_versions, dict):
            raise StoreError("invalid_publication_contract")
        for key, value in source_versions.items():
            self.policy._reference(key)
            if isinstance(value, list) and len(value) <= 64:
                for item in value:
                    self.policy._reference(item)
            else:
                self.policy._reference(value)
        publication_id = "publication_" + uuid.uuid4().hex
        with self._transaction():
            head = self.connection.execute(
                "SELECT generation FROM partition_head WHERE logical_rule_id=? AND partition_id=?", (rule_id, partition_id)
            ).fetchone()
            generation = head[0] if head else 0
            if base_generation is not None and base_generation != generation:
                raise PublicationConflict("partition_generation_changed")
            self.connection.execute(
                "INSERT INTO publication(publication_id,logical_rule_id,revision,partition_id,source_versions,base_generation,state) VALUES(?,?,?,?,?,?,'staging')",
                (publication_id, rule_id, revision, partition_id, canonical(source_versions), generation),
            )
            if replace_rule:
                self.connection.execute("INSERT INTO rule_generation VALUES(?,?)",
                                        (publication_id, canonical(self._rule_heads(rule_id))))
        return publication_id

    def _rule_heads(self, rule_id: str) -> list[list[Any]]:
        # Only internal publication IDs and counters are durable. Legacy
        # partition IDs may contain source-derived hashes and must not be copied.
        return [list(row) for row in self.connection.execute(
            "SELECT publication_id,generation,valid FROM partition_head WHERE logical_rule_id=? ORDER BY publication_id",
            (rule_id,))]

    def _is_rule_generation(self, publication_id: str) -> bool:
        return self.connection.execute("SELECT 1 FROM rule_generation WHERE publication_id=?",
                                       (publication_id,)).fetchone() is not None

    def replaced_publications(self, publication_id: str) -> list[str]:
        """Current old publications removed by the specified staged replacement."""
        pub = self._publication(publication_id)
        if self._is_rule_generation(publication_id):
            rows = self.connection.execute(
                "SELECT publication_id FROM partition_head WHERE logical_rule_id=? ORDER BY publication_id",
                (pub["logical_rule_id"],))
        else:
            rows = self.connection.execute(
                "SELECT publication_id FROM partition_head WHERE logical_rule_id=? AND partition_id=?",
                (pub["logical_rule_id"], pub["partition_id"]))
        return [row[0] for row in rows if row[0] != publication_id]

    def stage_fact(self, publication_id: str, fact: GraphFact, support: SupportSpec) -> None:
        self.policy.check_fact(fact)
        if not isinstance(support, SupportSpec):
            raise PersistenceViolation("typed_support_required")
        if support.origin == "database_rule" and not support.validation_ref:
            raise PersistenceViolation("database_support_requires_validation")
        if support.origin == "document_claim" and not support.document_ref:
            raise PersistenceViolation("document_support_requires_revision_and_section")
        if fact.fact_kind == "Edge" and support.status not in self.policy.relation_states(fact.payload["relation"]):
            raise PersistenceViolation("relation_status_not_permitted")
        with self._transaction():
            publication = self._publication(publication_id)
            if publication["state"] != "staging":
                raise StoreError("publication_not_staging")
            if (support.rule_id, support.rule_revision, support.partition_id) != (
                    publication["logical_rule_id"], publication["revision"], publication["partition_id"]):
                raise StoreError("support_publication_mismatch")
            self._validate_support_binding(fact, support)
            self.connection.execute("INSERT OR IGNORE INTO graph_fact VALUES(?,?,?,?)",
                                    (fact.fact_id, fact.isolation_domain, fact.fact_kind, canonical(fact.payload)))
            support_id = support.support_id(fact.fact_id)
            self.connection.execute("INSERT OR IGNORE INTO fact_support VALUES(?,?,?,?,?,?,?,?)", (
                support_id, fact.fact_id, support.origin, support.source_binding, support.scope_ref,
                support.partition_id, support.rule_id, support.rule_revision,
            ))
            existing = self.connection.execute(
                "SELECT validation_ref,document_ref,binding_revision,status FROM publication_support WHERE publication_id=? AND support_id=?",
                (publication_id, support_id),
            ).fetchone()
            evidence = (support.validation_ref, support.document_ref, support.binding_revision, support.status)
            if existing and tuple(existing) != evidence:
                raise StoreError("publication_support_is_immutable")
            self.connection.execute("INSERT OR IGNORE INTO publication_support VALUES(?,?,?,?,?,?)",
                                    (publication_id, support_id, *evidence))
            self.connection.execute("INSERT OR IGNORE INTO publication_binding VALUES(?,?,?)",
                                    (publication_id, support.source_binding, support.binding_revision))

    def _validate_support_binding(self, fact: GraphFact, support: SupportSpec) -> None:
        known = self.identity.lookup(support.source_binding)
        if not known:
            return  # Catalog, relation projections and documents use logical locators.
        binding = self.identity.lookup(support.source_binding, support.binding_revision)
        if not binding:
            raise StoreError("binding_revision_not_registered")
        payload = fact.payload
        if fact.fact_kind in {"NodeExistence", "PropertyValue"} and payload["entity_id"] != binding.entity_id:
            raise StoreError("binding_revision_entity_mismatch")
        if fact.fact_kind == "Edge" and binding.entity_id not in {payload["source"], payload["target"]}:
            raise StoreError("binding_revision_endpoint_mismatch")
        identity = self.connection.execute("SELECT isolation_domain,kind FROM entity_identity WHERE entity_id=?", (binding.entity_id,)).fetchone()
        if identity["isolation_domain"] != fact.isolation_domain:
            raise StoreError("binding_isolation_domain_mismatch")
        if fact.fact_kind == "NodeExistence" and identity["kind"] != payload["kind"]:
            raise StoreError("binding_entity_kind_mismatch")

    def _publication(self, publication_id: str) -> sqlite3.Row:
        result = self.connection.execute("SELECT * FROM publication WHERE publication_id=?", (publication_id,)).fetchone()
        if not result:
            raise StoreError("publication_not_found")
        return result

    def canary_check(self, publication_id: str) -> dict[str, Any]:
        """Check staged contracts and endpoints without advancing the published head."""
        with self._lock:
            publication = self._publication(publication_id)
            if publication["state"] not in {"staging", "ready"}:
                raise StoreError("canary_requires_staging")
            ids = self._prospective_publications([publication])
            self._validate_endpoints(ids)
            counts = self.connection.execute(
                "SELECT COUNT(DISTINCT fs.fact_id),COUNT(*) FROM publication_support ps JOIN fact_support fs USING(support_id) WHERE ps.publication_id=?",
                (publication_id,),
            ).fetchone()
            return {"publication_id": publication_id, "facts": counts[0], "supports": counts[1], "published": False}

    def publish_partition(self, completed: PartitionCompletion) -> dict[str, Any]:
        return self.publish_partitions([completed])[0]

    def publish_partitions(self, completed: Iterable[PartitionCompletion], *, expected_heads=None) -> list[dict[str, Any]]:
        """Publish a dependency closure in one transaction, including the watermark."""
        completions = list(completed)
        if not completions:
            raise StoreError("empty_publication_group")
        for item in completions:
            if not isinstance(item, PartitionCompletion):
                raise StoreError("typed_partition_completion_required")
            if not item.complete or not item.consistent or not item.dependencies_valid or item.truncated:
                raise IncompletePartition("partition_not_complete_consistent_current")
            if item.checkpoint_ref:
                self.policy._reference(item.checkpoint_ref)
        with self._transaction():
            if expected_heads is not None and canonical(expected_heads) != canonical(self.heads()):
                raise PublicationConflict("graph_generation_changed")
            publications = [self._publication(c.publication_id) for c in completions]
            keys = {(p["logical_rule_id"], p["partition_id"]) for p in publications}
            if len(keys) != len(publications):
                raise PublicationConflict("duplicate_partition_in_publish_group")
            full_rules = {p["logical_rule_id"] for p in publications
                          if self._is_rule_generation(p["publication_id"])}
            if any(sum(p["logical_rule_id"] == rid for p in publications) != 1 for rid in full_rules):
                raise PublicationConflict("overlapping_rule_generation_in_publish_group")
            for pub in publications:
                if pub["state"] == "aborted":
                    raise StoreError("publication_aborted")
                head = self.connection.execute(
                    "SELECT publication_id,generation FROM partition_head WHERE logical_rule_id=? AND partition_id=?",
                    (pub["logical_rule_id"], pub["partition_id"]),
                ).fetchone()
                if pub["state"] == "published":
                    if not head or head[0] != pub["publication_id"]:
                        raise PublicationConflict("historical_publication_cannot_be_reactivated")
                    continue
                generation = head[1] if head else 0
                if generation != pub["base_generation"]:
                    raise PublicationConflict("partition_generation_changed")
                full = self.connection.execute("SELECT base_heads FROM rule_generation WHERE publication_id=?",
                                               (pub["publication_id"],)).fetchone()
                if full and json.loads(full[0]) != self._rule_heads(pub["logical_rule_id"]):
                    raise PublicationConflict("rule_generation_changed")
                rule = self.connection.execute("SELECT dependency_state FROM rule WHERE rule_id=? AND revision=?",
                                               (pub["logical_rule_id"], pub["revision"])).fetchone()
                if rule and rule[0] != "valid":
                    raise StoreError("rule_dependencies_stale")
            self._validate_endpoints(self._prospective_publications(publications))
            from ..identity.migration import plan_identity_migration, record_identity_migration
            migration=plan_identity_migration(self,[p["publication_id"] for p in publications])
            if migration:
                record_identity_migration(self,migration)
            results = []
            for pub, completion in zip(publications, completions):
                if pub["state"] != "published":
                    generation = pub["base_generation"] + 1
                    self.connection.execute("UPDATE publication SET state='published',checkpoint_ref=?,published_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE publication_id=?",
                                            (completion.checkpoint_ref, pub["publication_id"]))
                    if pub["logical_rule_id"] in full_rules:
                        # History remains in publication/support tables. Removing
                        # obsolete heads is part of the same visibility change.
                        self.connection.execute("DELETE FROM partition_head WHERE logical_rule_id=?",
                                                (pub["logical_rule_id"],))
                    self.connection.execute("""INSERT INTO partition_head VALUES(?,?,?,?,?,1)
                      ON CONFLICT(logical_rule_id,partition_id) DO UPDATE SET
                      publication_id=excluded.publication_id,generation=excluded.generation,
                      checkpoint_ref=excluded.checkpoint_ref,valid=1""",
                                            (pub["logical_rule_id"], pub["partition_id"], pub["publication_id"], generation, completion.checkpoint_ref))
                else:
                    generation = pub["base_generation"] + 1
                results.append({"publication_id": pub["publication_id"], "generation": generation, "state": "published"})
            self._advance_watermarks()
            return results

    def _prospective_publications(self, replacements: list[sqlite3.Row]) -> list[str]:
        replacement_keys = {(p["logical_rule_id"], p["partition_id"]) for p in replacements}
        full_rules = {p["logical_rule_id"] for p in replacements
                      if self._is_rule_generation(p["publication_id"])}
        current = [r["publication_id"] for r in self.connection.execute("SELECT * FROM partition_head WHERE valid=1")
                   if r["logical_rule_id"] not in full_rules
                   and (r["logical_rule_id"], r["partition_id"]) not in replacement_keys]
        return current + [p["publication_id"] for p in replacements]

    def _validate_endpoints(self, publication_ids: list[str]) -> None:
        rows = self._fact_rows(publication_ids)
        # Validate the replacement together with all surviving heads. A cycle
        # can span rules, so checking each incoming rule in isolation is unsafe.
        from ..rules.topology import validate_hierarchy_rows
        validate_hierarchy_rows(rows, rules=self.get_rules(include_stale=True))
        from ..identity.migration import validate_identity_domain_rows
        validate_identity_domain_rows(self,rows)
        nodes = {}
        accepted_nodes = set()
        for row in rows:
            if row["fact_kind"] != "NodeExistence" or row["status"] == "deprecated":
                continue
            payload = json.loads(row["payload"])
            key = (row["isolation_domain"], payload["entity_id"])
            if key in nodes and nodes[key] != payload["kind"]:
                raise StoreError("conflicting_entity_kinds")
            nodes[key] = payload["kind"]
            if row["status"] == "accepted":
                accepted_nodes.add(key)
        for row in rows:
            if row["status"] == "deprecated":
                continue
            payload = json.loads(row["payload"])
            domain = row["isolation_domain"]
            if row["fact_kind"] == "Edge":
                source, target = (domain, payload["source"]), (domain, payload["target"])
                required_nodes = accepted_nodes if row["status"] == "accepted" else nodes
                if source not in required_nodes or target not in required_nodes:
                    raise StoreError("dangling_edge_requires_dependency_publication")
                source_kinds, target_kinds = self.policy.relation_endpoint_kinds(payload["relation"])
                if nodes[source] not in source_kinds or nodes[target] not in target_kinds:
                    raise StoreError("relation_endpoint_kind_mismatch")
            if row["fact_kind"] == "PropertyValue" and (domain, payload["entity_id"]) not in (accepted_nodes if row["status"] == "accepted" else nodes):
                raise StoreError("property_requires_visible_node_support")
            if row["fact_kind"] == "PropertyValue":
                self.policy.check_property_kind(payload["name"], nodes[(domain, payload["entity_id"])])
        if publication_ids:
            placeholders = ",".join("?" for _ in publication_ids)
            conflicting = self.connection.execute(f"""SELECT pb.binding_id FROM publication_binding pb
              JOIN source_binding_revision br ON br.binding_id=pb.binding_id AND br.revision=pb.binding_revision
              WHERE pb.publication_id IN ({placeholders}) GROUP BY pb.binding_id
              HAVING COUNT(DISTINCT br.entity_id)>1 LIMIT 1""", publication_ids).fetchone()
            if conflicting:
                raise StoreError("binding_revision_requires_dependency_publication")

    def abort(self, publication_id: str) -> None:
        with self._transaction():
            pub = self._publication(publication_id)
            if pub["state"] == "published":
                raise StoreError("cannot_abort_published_partition")
            self.connection.execute("UPDATE publication SET state='aborted' WHERE publication_id=?", (publication_id,))

    def recover_staging(self) -> list[str]:
        """Administrative recovery: discard unfinished work; callers requery its source."""
        with self._transaction():
            ids = [r[0] for r in self.connection.execute("SELECT publication_id FROM publication WHERE state IN ('staging','ready')")]
            self.connection.execute("UPDATE publication SET state='aborted' WHERE state IN ('staging','ready')")
            return ids

    def _fact_rows(self, publication_ids: list[str]) -> list[sqlite3.Row]:
        if not publication_ids:
            return []
        placeholders = ",".join("?" for _ in publication_ids)
        return list(self.connection.execute(f"""SELECT gf.*,fs.support_id,fs.origin,fs.binding_id,fs.scope_ref,
          fs.partition_id,fs.rule_id,fs.revision,ps.publication_id,ps.validation_ref,ps.document_ref,
          ps.binding_revision,ps.status FROM publication_support ps
          JOIN fact_support fs USING(support_id) JOIN graph_fact gf USING(fact_id)
          WHERE ps.publication_id IN ({placeholders}) ORDER BY gf.fact_id,fs.support_id,ps.publication_id""", publication_ids))

    def view(self, scopes: set[str], domain: str = "default", publication_id: str | None = None,
             include_candidates: bool = False, apply_resolutions: bool = True) -> dict[str, Any]:
        """Authorize individual supports before labels, search text or aggregation."""
        if not isinstance(scopes, (set, frozenset)) or not all(isinstance(s, str) for s in scopes):
            raise StoreError("explicit_scope_set_required")
        with self._lock:
            self.connection.execute("BEGIN")
            try:
                if publication_id:
                    pub = self._publication(publication_id)
                    if pub["state"] != "published":
                        raise StoreError("only_published_history_visible")
                    publication_ids = [publication_id]
                else:
                    publication_ids = [r[0] for r in self.connection.execute("SELECT publication_id FROM partition_head WHERE valid=1 ORDER BY logical_rule_id,partition_id")]
                rows = self._fact_rows(publication_ids)
                rows = [r for r in rows if r["isolation_domain"] == domain and r["scope_ref"] in scopes
                        and (r["status"] == "accepted" or (include_candidates and r["status"] in {"candidate", "unresolved"}))]
                if apply_resolutions and not publication_id:
                    from ..identity.reconciliation import project_resolutions
                    rows = project_resolutions(self, rows, scopes, domain)
                result = self._aggregate(rows)
                schema = self.policy.schema_for_facts(result["facts"])
                if not schema.is_empty():
                    result["semantic_schema"] = schema.model_dump(mode="json")
                visible_publications = sorted({r["publication_id"] for r in rows})
                result["publications"] = visible_publications
                result["historical"] = bool(publication_id)
                return result
            finally:
                self.connection.execute("ROLLBACK")

    def _aggregate(self, rows: list[sqlite3.Row]) -> dict[str, Any]:
        facts: dict[str, dict[str, Any]] = {}
        supports: dict[str, dict[str, Any]] = {}
        priority = {"accepted": 3, "candidate": 2, "unresolved": 1}
        for row in rows:
            payload = json.loads(row["payload"])
            fact = facts.setdefault(row["fact_id"], {"id": row["fact_id"], "fact_kind": row["fact_kind"],
                                  "payload": payload, "status": row["status"], "support_ids": []})
            if priority.get(row["status"], 0) > priority.get(fact["status"], 0):
                fact["status"] = row["status"]
            if row["support_id"] not in fact["support_ids"]:
                fact["support_ids"].append(row["support_id"])
            support_view_id = row["publication_id"] + ":" + row["support_id"]
            supports[support_view_id] = {
                "id": row["support_id"], "fact_id": row["fact_id"], "origin": row["origin"],
                "source_binding": row["binding_id"], "scope_ref": row["scope_ref"],
                "partition_id": row["partition_id"], "rule_id": row["rule_id"], "rule_revision": row["revision"],
                "publication_id": row["publication_id"], "validation_ref": row["validation_ref"],
                "document_ref": row["document_ref"], "binding_revision": row["binding_revision"], "status": row["status"],
            }
        nodes: dict[str, dict[str, Any]] = {}
        for fact in facts.values():
            if fact["fact_kind"] != "NodeExistence":
                continue
            payload = fact["payload"]
            node = nodes.setdefault(payload["entity_id"], {"id": payload["entity_id"], "kind": payload["kind"],
                "layer": payload["layer"], "label": payload["entity_id"], "aliases": [], "properties": {},
                "status": fact["status"], "support_ids": [], "conflicts": []})
            node["support_ids"].extend(fact["support_ids"])
        properties: dict[tuple[str, str], list[Any]] = {}
        retained = set()
        for fact in facts.values():
            if fact["fact_kind"] == "NodeExistence":
                retained.add(fact["id"])
            elif fact["fact_kind"] == "PropertyValue":
                p = fact["payload"]
                if p["entity_id"] not in nodes:
                    continue
                retained.add(fact["id"])
                key = (p["entity_id"], p["name"])
                values = properties.setdefault(key, [])
                if canonical(p["value"]) not in {canonical(v) for v in values}:
                    values.append(p["value"])
                nodes[p["entity_id"]]["support_ids"].extend(fact["support_ids"])
        for (entity_id, name), values in sorted(properties.items()):
            node = nodes[entity_id]
            values.sort(key=canonical)
            if name == "alias":
                node["aliases"] = values
            elif name == "label":
                if len(values) == 1:
                    node["label"] = values[0]
                else:
                    node["conflicts"].append({"property": "label", "values": values})
            else:
                node["properties"][name] = values[0] if len(values) == 1 else values
                definition = self.policy.property_definitions.get(name)
                if len(values) > 1 and name not in {"business_code", "english_name", "level", "level_no", "source_position"} and not (definition and definition.multi_valued):
                    node["conflicts"].append({"property": name, "values": values})
        edges, claims = [], []
        for fact in facts.values():
            p = fact["payload"]
            if fact["fact_kind"] == "Edge" and p["source"] in nodes and p["target"] in nodes:
                retained.add(fact["id"])
                edges.append({"id": fact["id"], **p, "status": fact["status"], "support_ids": sorted(fact["support_ids"])})
            elif fact["fact_kind"] == "DocumentClaim":
                retained.add(fact["id"])
                claims.append({**p, "fact_id": fact["id"], "status": fact["status"], "support_ids": sorted(fact["support_ids"])})
        for node in nodes.values():
            node["support_ids"] = sorted(set(node["support_ids"]))
        return {"nodes": sorted(nodes.values(), key=lambda n: n["id"]), "edges": sorted(edges, key=lambda e: e["id"]),
                "claims": sorted(claims, key=lambda c: c["id"]),
                "facts": [facts[fid] for fid in sorted(retained)],
                "supports": sorted((s for s in supports.values() if s["fact_id"] in retained), key=lambda s: (s["id"], s["publication_id"]))}

    def hierarchy_view(self, scopes, definition, *, domain="default", publication_id=None):
        """Read a rooted projection without changing identities or parent facts."""
        from ..rules.views import rooted_view
        return rooted_view(self, scopes, definition, domain=domain, publication_id=publication_id)

    def search(self, text: str, scopes: set[str], domain: str = "default") -> list[dict[str, Any]]:
        view = self.view(scopes, domain)
        needle = text.casefold()
        return [node for node in view["nodes"] if needle in canonical({"label": node["label"], "aliases": node["aliases"], "properties": node["properties"]}).casefold()]

    def export(self, scopes: set[str], domain: str = "default", publication_id: str | None = None) -> dict[str, Any]:
        """Return one coherent ACL-filtered snapshot for a caller-owned output writer."""
        return self.view(scopes, domain, publication_id)

    def put_rule(self, rule: RuleSpec) -> None:
        self.policy.check_record(rule)
        payload = rule.model_dump(mode="json", by_alias=True)
        payload.pop("status")
        serialized = canonical(payload)
        with self._lock:
            old = self.connection.execute("SELECT payload FROM rule WHERE rule_id=? AND revision=?", (rule.rule_id, rule.revision)).fetchone()
            # New optional contracts have defaults. An old record missing those
            # fields is equivalent to its normalized model, not a new revision.
            previous = (canonical(RuleSpec.model_validate_json(old[0]).model_dump(mode="json", by_alias=True,
                                                                                exclude={"status"})) if old else None)
            if previous is not None and previous != serialized:
                raise StoreError("rule_revision_is_immutable")
            self.register_rule_semantics(rule)
            with self._transaction():
                self.connection.execute("INSERT INTO rule VALUES(?,?,?,?, 'valid') ON CONFLICT(rule_id,revision) DO UPDATE SET lifecycle=excluded.lifecycle",
                                        (rule.rule_id, rule.revision, serialized, rule.status))

    def get_rules(self, *, include_stale: bool = False, scopes: set[str] | None = None,
                  domain: str = "default") -> list[RuleSpec]:
        allowed = None
        if scopes is not None:
            allowed = {(s["rule_id"], s["rule_revision"]) for s in self.view(scopes, domain)["supports"]}
        rows = self.connection.execute("SELECT * FROM rule ORDER BY rule_id,revision")
        return [RuleSpec.model_validate({**json.loads(r["payload"]), "status": r["lifecycle"]}) for r in rows
                if (include_stale or r["dependency_state"] == "valid") and (allowed is None or (r["rule_id"], r["revision"]) in allowed)]

    def put_validation(self, record: ValidationRecord) -> None:
        self.policy.check_record(record)
        serialized = canonical(record)
        with self._transaction():
            old = self.connection.execute("SELECT payload FROM validation_record WHERE validation_id=?", (record.validation_id,)).fetchone()
            if old and old[0] != serialized:
                raise StoreError("validation_is_immutable")
            self.connection.execute("INSERT OR IGNORE INTO validation_record VALUES(?,?)", (record.validation_id, serialized))

    def invalidate_rule(self, rule_id: str, reason_code: str = "dependency_changed") -> list[str]:
        """Invalidate dependent results as a closure; keep historical publications."""
        rules = self.get_rules(include_stale=True)
        affected = {rule_id}
        changed = True
        while changed:
            changed = False
            for rule in rules:
                if rule.relation and {rule.relation.source_rule_id, rule.relation.target_rule_id} & affected and rule.rule_id not in affected:
                    affected.add(rule.rule_id)
                    changed = True
        with self._transaction():
            for rid in sorted(affected):
                self.connection.execute("UPDATE rule SET dependency_state='stale' WHERE rule_id=?", (rid,))
                self.connection.execute("UPDATE partition_head SET valid=0 WHERE logical_rule_id=?", (rid,))
                self.connection.execute("INSERT INTO rule_event VALUES(?,?,?,?,?)", (uuid.uuid4().hex, rid, 0, "invalidate", reason_code))
        return sorted(affected)

    def mark_rule_for_review(self, rule_id: str, reason_code: str) -> list[str]:
        """Hide invalid current results while retaining executable rules and history.

        A data counterexample differs from a stale schema/document dependency:
        the same rule may become valid again after the source is corrected.
        """
        self.policy._reference(rule_id)
        self.policy._reference(reason_code)
        latest = {rule.rule_id: rule for rule in self.get_rules(include_stale=True)}
        affected = {rule_id}
        while True:
            dependents = {rule.rule_id for rule in latest.values() if rule.relation and
                          {rule.relation.source_rule_id, rule.relation.target_rule_id} & affected}
            if dependents <= affected:
                break
            affected.update(dependents)
        with self._transaction():
            for rid in sorted(affected):
                changed = self.connection.execute(
                    "UPDATE partition_head SET valid=0 WHERE logical_rule_id=? AND valid=1", (rid,)).rowcount
                recorded = self.connection.execute(
                    "SELECT 1 FROM rule_event WHERE rule_id=? AND event_type='review_required' AND reason_code=?",
                    (rid, reason_code)).fetchone()
                if changed or not recorded:
                    revision = latest[rid].revision if rid in latest else 0
                    self.connection.execute("INSERT INTO rule_event VALUES(?,?,?,?,?)",
                        (uuid.uuid4().hex, rid, revision, "review_required", reason_code))
        return sorted(affected)

    def prepare_rule_rollback(self, rule_id: str, revision: int, current_dependencies: dict[str, Any]) -> RuleSpec:
        """Return a compatible prior definition; only re-execution may publish it."""
        candidates = [r for r in self.get_rules(include_stale=True) if r.rule_id == rule_id and r.revision == revision]
        if not candidates:
            raise StoreError("rollback_rule_not_found")
        rule = candidates[0]
        if canonical(rule.dependencies.model_dump(mode="json", by_alias=True)) != canonical(current_dependencies):
            raise StoreError("rollback_dependencies_incompatible")
        with self._transaction():
            self.connection.execute("INSERT INTO rule_event VALUES(?,?,?,?,?)", (uuid.uuid4().hex, rule_id, revision, "rollback_requested", "requires_current_source_revalidation"))
        return rule

    def register_window(self, window_id: str, source_ref: str, checkpoint_ref: str,
                        partitions: list[tuple[str, str]], *, enumeration_complete: bool) -> None:
        for ref in (window_id, source_ref, checkpoint_ref):
            self.policy._reference(ref)
        if not partitions or len(partitions) != len(set(partitions)):
            raise StoreError("window_requires_unique_partition_manifest")
        with self._transaction():
            self.connection.execute("INSERT INTO source_window(window_id,source_ref,checkpoint_ref,enumeration_complete) VALUES(?,?,?,?)",
                                    (window_id, source_ref, checkpoint_ref, int(enumeration_complete)))
            self.connection.executemany("INSERT INTO window_partition(window_id,rule_id,partition_id) VALUES(?,?,?)",
                                        [(window_id, rule, partition) for rule, partition in partitions])

    def attach_window_publication(self, window_id: str, publication_id: str) -> None:
        with self._transaction():
            pub = self._publication(publication_id)
            result = self.connection.execute("UPDATE window_partition SET publication_id=? WHERE window_id=? AND rule_id=? AND partition_id=?",
                                             (publication_id, window_id, pub["logical_rule_id"], pub["partition_id"]))
            if result.rowcount != 1:
                raise StoreError("publication_outside_window_manifest")
            self._advance_watermarks()

    def _advance_watermarks(self) -> None:
        for window in self.connection.execute("SELECT rowid AS window_order,* FROM source_window WHERE state='pending' AND enumeration_complete=1 ORDER BY rowid").fetchall():
            earlier = self.connection.execute(
                "SELECT COUNT(*) FROM source_window WHERE source_ref=? AND rowid<? AND state='pending'",
                (window["source_ref"], window["window_order"]),
            ).fetchone()[0]
            if earlier:
                continue
            missing = self.connection.execute("""SELECT COUNT(*) FROM window_partition wp
              LEFT JOIN publication p ON p.publication_id=wp.publication_id
              WHERE wp.window_id=? AND (wp.publication_id IS NULL OR p.state IS NULL OR p.state!='published')""", (window["window_id"],)).fetchone()[0]
            if missing == 0:
                self.connection.execute("INSERT INTO source_watermark VALUES(?,?,?) ON CONFLICT(source_ref) DO UPDATE SET window_id=excluded.window_id,checkpoint_ref=excluded.checkpoint_ref",
                                        (window["source_ref"], window["window_id"], window["checkpoint_ref"]))
                self.connection.execute("UPDATE source_window SET state='published' WHERE window_id=?", (window["window_id"],))

    def watermark(self, source_ref: str) -> dict[str, str] | None:
        row = self.connection.execute("SELECT * FROM source_watermark WHERE source_ref=?", (source_ref,)).fetchone()
        return dict(row) if row else None

    def heads(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM partition_head ORDER BY logical_rule_id,partition_id")]

    def current_versions(self, rule_id: str, partition_id: str = "all") -> dict | None:
        """Read only committed dependency versions; never expose staging metadata."""
        row=self.connection.execute("""SELECT p.source_versions FROM partition_head h
            JOIN publication p USING(publication_id)
            WHERE h.logical_rule_id=? AND h.partition_id=? AND h.valid=1""",(rule_id,partition_id)).fetchone()
        return json.loads(row[0]) if row else None
