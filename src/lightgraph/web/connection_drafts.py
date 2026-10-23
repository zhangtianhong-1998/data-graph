"""Bounded, incomplete connection forms kept separate from executable profiles."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


DatabaseKind = Literal["gauss_database", "mysql_database"]
DraftRevision = Annotated[int, Field(ge=0, le=2**53 - 1)]


class _DraftModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class DraftSourceForm(_DraftModel):
    id: str = Field(max_length=64)
    kind: DatabaseKind
    dsnEnv: str = Field(max_length=256)
    tables: str = Field(max_length=16000)
    capabilityManifest: str = Field(max_length=1024)


class DraftConnectionForm(_DraftModel):
    kind: DatabaseKind
    format: Literal["fields", "dsn"]
    dsn: SecretStr = Field(max_length=16000)
    host: str = Field(max_length=512)
    port: str = Field(max_length=16)
    database: str = Field(max_length=512)
    username: str = Field(max_length=512)
    password: SecretStr = Field(max_length=4096)
    schema_name: str = Field(alias="schema", max_length=512)
    sslmode: Literal["auto", "disable", "allow", "prefer", "require", "verify-ca", "verify-full"] = "auto"


class DatabaseDraft(_DraftModel):
    """Allow unfinished input without normalizing values or testing connectivity."""

    form: DraftSourceForm
    connection: DraftConnectionForm
    mode: Literal["direct", "environment"]
    retention: bool
    checked: list[Annotated[str, Field(max_length=256)]] = Field(max_length=1000)

    @model_validator(mode="after")
    def matching_kinds(self) -> DatabaseDraft:
        if self.form.kind != self.connection.kind:
            raise ValueError("draft_connection_kind_mismatch")
        return self

    def to_payload(self) -> dict:
        """Unwrap only for encryption or the authenticated no-store edit response."""
        payload = self.model_dump(by_alias=True)
        for field in ("dsn", "password"):
            payload["connection"][field] = getattr(self.connection, field).get_secret_value()
        return payload


class SaveConnectionDraft(_DraftModel):
    draft: DatabaseDraft
    revision: DraftRevision
