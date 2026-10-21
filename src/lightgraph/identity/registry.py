"""Keep accepted business identities separate from tenant-local lookup keys.

The registry never treats equal names as proof of identity. A caller may opt in
to definition sharing only after validating the definition registry or an
explicit same-entity mapping. Published binding revisions remain immutable.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from lightgraph.contracts import canonical, digest
from lightgraph.errors import LightGraphError


class IdentityConflict(LightGraphError):
    pass


@dataclass(frozen=True)
class IdentityResolution:
    entity_id: str
    binding_id: str
    revision: int
    status: str = "accepted"


DEFINITION_FIELDS = frozenset({
    "label", "name", "definition", "unit", "formula", "period", "caliber",
    "granularity", "semantic_key", "canonical_code", "taxonomy", "level",
    "aggregation", "dependencies", "namespace", "description", "code",
})
STRONG_FIELDS = DEFINITION_FIELDS - {"label", "name", "description"}


def _safe_definition(definition: dict[str, Any]) -> dict[str, Any]:
    if set(definition) - DEFINITION_FIELDS:
        raise IdentityConflict("definition_field_not_allowed")
    for value in definition.values():
        if isinstance(value, list):
            if len(value) > 64 or any(not isinstance(v, (str, int, float, bool, type(None))) for v in value):
                raise IdentityConflict("invalid_definition_value")
        elif not isinstance(value, (str, int, float, bool, type(None))):
            raise IdentityConflict("invalid_definition_value")
    canonical(definition)  # Reject NaN, preserving type, whitespace and leading zeroes.
    return dict(definition)


class IdentityRegistry:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS entity_identity (
          entity_id TEXT PRIMARY KEY, isolation_domain TEXT NOT NULL,
          kind TEXT NOT NULL, semantic_namespace TEXT NOT NULL,
          definition TEXT NOT NULL, semantic_signature TEXT,
          redirect_to TEXT, identity_revision INTEGER NOT NULL DEFAULT 1
        );
        CREATE UNIQUE INDEX IF NOT EXISTS accepted_signature
          ON entity_identity(isolation_domain, kind, semantic_namespace, semantic_signature)
          WHERE semantic_signature IS NOT NULL AND redirect_to IS NULL;
        CREATE TABLE IF NOT EXISTS source_binding (
          binding_id TEXT PRIMARY KEY, isolation_domain TEXT NOT NULL,
          source_ref TEXT NOT NULL, namespace_ref TEXT NOT NULL,
          accepted_business_key TEXT NOT NULL, definition_version TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS source_binding_revision (
          binding_id TEXT NOT NULL REFERENCES source_binding(binding_id),
          revision INTEGER NOT NULL, entity_id TEXT NOT NULL REFERENCES entity_identity(entity_id),
          definition TEXT NOT NULL,
          resolution_policy TEXT NOT NULL DEFAULT 'source_scoped',
          semantic_namespace TEXT NOT NULL DEFAULT '', semantic_signature TEXT,
          PRIMARY KEY(binding_id, revision)
        );
        """)
        # Older result stores kept policy only implicitly on entity_identity.
        # Preserve every historical binding while making policy changes explicit.
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(source_binding_revision)")}
        additions = {
            "resolution_policy": "TEXT NOT NULL DEFAULT 'source_scoped'",
            "semantic_namespace": "TEXT NOT NULL DEFAULT ''",
            "semantic_signature": "TEXT",
        }
        for name, declaration in additions.items():
            if name in columns:
                continue
            self.connection.execute(f"ALTER TABLE source_binding_revision ADD COLUMN {name} {declaration}")
            if name == "resolution_policy":
                self.connection.execute("""UPDATE source_binding_revision SET resolution_policy =
                    CASE WHEN (SELECT semantic_signature FROM entity_identity e
                               WHERE e.entity_id=source_binding_revision.entity_id) IS NOT NULL
                    THEN 'verified_definition_registry' ELSE 'source_scoped' END""")
            else:
                self.connection.execute(f"""UPDATE source_binding_revision SET {name} =
                    (SELECT {name} FROM entity_identity e WHERE e.entity_id=source_binding_revision.entity_id)""")

    def resolve(self, *, kind: str, source_ref: str, namespace: Any,
                business_key: Any, definition: dict[str, Any] | None = None,
                isolation_domain: str = "default", definition_version: str = "",
                shared_verified: bool = False, semantic_namespace: str = "",
                identity_domain: str = "", verified_key: Any = None,
                identity_evidence: tuple[str, ...] = ()) -> IdentityResolution:
        """Resolve one accepted semantic key; definition changes create a binding revision.

        A changed label retains its entity ID. A changed defining attribute gets
        a separate identity, leaving old publication bindings reproducible.
        """
        definition = _safe_definition(definition or {})
        self._validate_key(namespace)
        self._validate_key(business_key)
        binding_id = "binding_" + digest(isolation_domain, kind, source_ref, namespace, business_key, definition_version)
        history = self.connection.execute(
            """SELECT b.entity_id,b.revision,b.definition,b.resolution_policy,b.semantic_namespace,b.semantic_signature,e.redirect_to
               FROM source_binding_revision b JOIN entity_identity e ON e.entity_id=b.entity_id
               WHERE b.binding_id=? ORDER BY b.revision DESC""",
            (binding_id,),
        ).fetchall()
        latest = history[0] if history else None
        strong = {k: v for k, v in definition.items() if k in STRONG_FIELDS and v not in (None, "", [])}
        # Unit, period or taxonomy alone describes many unrelated entities.
        # Sharing needs an identifying definition, or a name plus independent
        # defining attributes under an explicitly verified registry policy.
        identifying = any(definition.get(k) not in (None, "", [])
                          for k in ("definition", "semantic_key", "canonical_code", "formula"))
        named_definition = bool(definition.get("label") or definition.get("name")) and bool(strong)
        signature_payload = ({k: v for k, v in definition.items() if k not in {"label", "name", "description"}}
                             if identifying else definition)
        signature = digest(signature_payload) if shared_verified and (identifying or named_definition) else None
        policy = "verified_definition_registry" if shared_verified else "source_scoped"
        if identity_domain:
            if verified_key is None or not identity_evidence:
                raise IdentityConflict("verified_identity_domain_evidence_required")
            self._validate_key(verified_key)
            semantic_namespace = "identity_domain:" + identity_domain
            signature = digest("verified_business_key", verified_key)
            policy = "verified_identity_domain"
            # Attribute agreement is checked over prospective published supports,
            # not this registry's draft history. A failed draft must not poison
            # later valid attempts to resolve the same accepted business key.

        def matches(row):
            old_definition = json.loads(row[2])
            old_strong = {k: v for k, v in old_definition.items() if k in STRONG_FIELDS and v not in (None, "", [])}
            return (canonical(old_strong) == canonical(strong)
                    and row[3] == policy and row[4] == semantic_namespace and row[5] == signature
                    and row[6] is None)

        if latest and matches(latest):
            return IdentityResolution(latest[0], binding_id, latest[1])
        entity_id = None
        if signature:
            found = self.connection.execute(
                "SELECT entity_id FROM entity_identity WHERE isolation_domain=? AND kind=? AND semantic_namespace=? AND semantic_signature=? AND redirect_to IS NULL",
                (isolation_domain, kind, semantic_namespace, signature),
            ).fetchone()
            entity_id = found[0] if found else None
        if not entity_id:
            # Failed staging can leave a newer binding revision. Returning to an
            # already known definition/policy must reuse its identity, not mint a
            # new entity merely because that revision was not the latest one.
            old = next((row for row in history if matches(row)), None)
            entity_id = old[0] if old else None
        if not entity_id:
            entity_id = "entity_" + uuid.uuid4().hex
            self.connection.execute(
                "INSERT INTO entity_identity(entity_id,isolation_domain,kind,semantic_namespace,definition,semantic_signature) VALUES(?,?,?,?,?,?)",
                (entity_id, isolation_domain, kind, semantic_namespace, canonical(definition), signature),
            )
        self.connection.execute(
            "INSERT OR IGNORE INTO source_binding VALUES(?,?,?,?,?,?)",
            (binding_id, isolation_domain, source_ref, canonical(namespace), canonical(business_key), definition_version),
        )
        revision = latest[1] + 1 if latest else 1
        self.connection.execute("""INSERT INTO source_binding_revision
            (binding_id,revision,entity_id,definition,resolution_policy,semantic_namespace,semantic_signature)
            VALUES(?,?,?,?,?,?,?)""",
            (binding_id, revision, entity_id, canonical(definition), policy, semantic_namespace, signature))
        return IdentityResolution(entity_id, binding_id, revision)

    def lookup(self, binding_id: str, revision: int | None = None) -> IdentityResolution | None:
        if revision is None:
            row = self.connection.execute(
                "SELECT entity_id,revision FROM source_binding_revision WHERE binding_id=? ORDER BY revision DESC LIMIT 1", (binding_id,)
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT entity_id,revision FROM source_binding_revision WHERE binding_id=? AND revision=?", (binding_id, revision)
            ).fetchone()
        return IdentityResolution(row[0], binding_id, row[1]) if row else None

    @staticmethod
    def _validate_key(value: Any) -> None:
        if isinstance(value, (list, tuple)):
            if len(value) > 64:
                raise IdentityConflict("identity_key_too_large")
            for item in value:
                IdentityRegistry._validate_key(item)
        elif isinstance(value, dict):
            if len(value) > 32:
                raise IdentityConflict("identity_key_too_large")
            for key, item in value.items():
                if not isinstance(key, str):
                    raise IdentityConflict("identity_key_invalid")
                IdentityRegistry._validate_key(item)
        elif not isinstance(value, (str, int, float, bool, type(None))):
            raise IdentityConflict("identity_key_invalid")
        canonical(value)
