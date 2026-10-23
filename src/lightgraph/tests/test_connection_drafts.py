"""Unfinished connection input survives refresh without creating a data source."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import stat

from cryptography.fernet import Fernet
from pydantic import ValidationError
import pytest

from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import SourceConfig
from lightgraph.web.connection_drafts import DatabaseDraft, SaveConnectionDraft
from lightgraph.web.database_access import ConnectionSpec
from lightgraph.web.saved_connections import SavedConnections


def draft(**connection_updates):
    connection = {"kind": "gauss_database", "format": "dsn",
                  "dsn": " jdbc:gaussdb://中文主机:8/未完成?password=秘密 ",
                  "host": "", "port": "8-", "database": "", "username": " 用户 ",
                  "password": "未完成密 码@:/?#&'\\\n", "schema": "", "sslmode": "auto"}
    connection.update(connection_updates)
    return DatabaseDraft.model_validate({
        "form": {"id": "", "kind": "gauss_database", "dsnEnv": "", "tables": " 表名\n",
                 "capabilityManifest": ""},
        "connection": connection, "mode": "direct", "retention": False,
        "checked": ["public.指标"],
    })


def save_profile(store, pid="alpha"):
    source = SourceConfig(id="db", kind="gauss_database", credential_mode="session",
                          credential_revision="v1")
    store.save(pid, source, ConnectionSpec(kind="gauss_database", dsn="host=example user=u"),
               "host=example user=u")


def test_incomplete_unicode_form_restarts_exactly_without_source_or_plaintext(tmp_path):
    root = tmp_path / "workspace"
    original = draft()
    assert SavedConnections(root).save_draft("alpha", "new", original, 12) == 12
    assert SavedConnections(root).load_draft("alpha", "new") == {
        "draft": original.to_payload(), "revision": 12}
    assert not (root / "alpha/.connections").exists()
    assert not (root / "alpha/config.yaml").exists()
    for path in root.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            for value in ("jdbc:gaussdb", "中文主机", "未完成密", "public.指标"):
                assert value.encode() not in data
    assert list(root.rglob("*.enc")) == [root / "alpha/.connection-drafts/new.enc"]
    assert "秘密" not in repr(original) and "未完成密" not in repr(original)
    assert "秘密" not in original.model_dump_json()


@pytest.mark.parametrize("revision", [-1, 2**53, True, "12", 1.5])
def test_revisions_are_strict_and_bounded(tmp_path, revision):
    with pytest.raises(ValidationError):
        SaveConnectionDraft(draft=draft(), revision=revision)
    with pytest.raises(LightGraphError, match="^saved_connection_invalid$"):
        SavedConnections(tmp_path).save_draft("alpha", "new", draft(), revision)
    with pytest.raises(LightGraphError, match="^saved_connection_invalid$"):
        SavedConnections(tmp_path).delete_draft("alpha", "new", revision)


def test_form_validation_does_not_accept_query_rows_or_coerce_input():
    value = draft().to_payload()
    value["rows"] = [{"secret": "RAW_QUERY_ROW"}]
    with pytest.raises(ValidationError) as captured:
        DatabaseDraft.model_validate(value)
    assert "RAW_QUERY_ROW" not in str(captured.value)
    value.pop("rows")
    value["connection"]["port"] = 5432
    with pytest.raises(ValidationError):
        DatabaseDraft.model_validate(value)
    value["connection"]["port"] = ""
    value["connection"]["password"] = "PASSWORD_VALUE" * 1000
    with pytest.raises(ValidationError) as captured:
        DatabaseDraft.model_validate(value)
    assert "PASSWORD_VALUE" not in str(captured.value)


def test_old_and_duplicate_autosaves_do_not_replace_newer_edits(tmp_path):
    store = SavedConnections(tmp_path)
    store.save_draft("alpha", "new", draft(password="new"), 20)
    assert store.save_draft("alpha", "new", draft(password="old"), 19) == 20
    assert store.save_draft("alpha", "new", draft(password="duplicate"), 20) == 20
    assert store.load_draft("alpha", "new")["draft"]["connection"]["password"] == "new"


def test_concurrent_requests_keep_maximum_revision(tmp_path):
    def save(revision):
        return SavedConnections(tmp_path).save_draft("alpha", "new", draft(password=str(revision)), revision)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(save, [9, 2, 7, 20, 8, 16, 4, 1]))
    result = SavedConnections(tmp_path).load_draft("alpha", "new")
    assert result["revision"] == 20
    assert result["draft"]["connection"]["password"] == "20"


def test_erasure_blocks_delayed_unload_save_but_allows_newer_edits(tmp_path):
    store = SavedConnections(tmp_path)
    store.save_draft("alpha", "new", draft(), 10)
    assert store.delete_draft("alpha", "new", 11) == 11
    assert store.load_draft("alpha", "new") is None
    assert SavedConnections(tmp_path).load_draft_revision("alpha", "new") == 11
    assert store.save_draft("alpha", "new", draft(), 10) == 11
    assert store.save_draft("alpha", "new", draft(), 11) == 11
    assert store.load_draft("alpha", "new") is None
    assert (tmp_path / "alpha/.connection-drafts/new.rev").read_text() == "11"
    assert store.save_draft("alpha", "new", draft(password="new"), 12) == 12
    assert store.load_draft_revision("alpha", "new") == 12
    assert store.load_draft("alpha", "new")["draft"]["connection"]["password"] == "new"
    assert store.save_draft("alpha", "new", draft(password="late"), 10) == 12
    # A delayed erase request must not discard subsequently entered input.
    assert store.delete_draft("alpha", "new", 11) == 12
    assert store.delete_draft("alpha", "new", 10) == 12
    assert store.load_draft("alpha", "new")["revision"] == 12


def test_project_and_dialog_scopes_are_independent(tmp_path):
    store = SavedConnections(tmp_path)
    for pid, key, password in [("alpha", "new", "a"), ("beta", "new", "b"),
                               ("alpha", "source-db", "c")]:
        store.save_draft(pid, key, draft(password=password), 1)
    store.delete_draft("alpha", "new", 2)
    assert store.load_draft("alpha", "new") is None
    assert store.load_draft("beta", "new")["draft"]["connection"]["password"] == "b"
    assert store.load_draft("alpha", "source-db")["draft"]["connection"]["password"] == "c"


@pytest.mark.parametrize("pid,key", [("beta", "new"), ("alpha", "source-other")])
def test_ciphertext_cannot_be_rebound_to_another_project_or_dialog(tmp_path, pid, key):
    store = SavedConnections(tmp_path)
    store.save_draft("alpha", "new", draft(), 2)
    target = tmp_path / pid / ".connection-drafts" / f"{key}.enc"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((tmp_path / "alpha/.connection-drafts/new.enc").read_bytes())
    assert store.load_draft(pid, key) is None


@pytest.mark.parametrize("pid,key", [("../bad", "new"), ("alpha", ""), ("alpha", "db"),
                                    ("alpha", "source-"), ("alpha", "source-../x"),
                                    ("alpha", "source-" + "x" * 129)])
def test_invalid_paths_never_create_files(tmp_path, pid, key):
    store = SavedConnections(tmp_path)
    for operation in (lambda: store.save_draft(pid, key, draft(), 1),
                      lambda: store.load_draft(pid, key),
                      lambda: store.delete_draft(pid, key)):
        with pytest.raises(LightGraphError, match="^saved_connection_invalid$"):
            operation()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("broken_key", [None, b"bad-key", b"bad-key" * 100])
def test_drafts_prevent_lost_key_rotation_and_remain_erasable(tmp_path, broken_key):
    store = SavedConnections(tmp_path)
    store.save_draft("alpha", "new", draft(), 1)
    key = tmp_path / ".connection-key"
    if broken_key is None:
        key.unlink()
    else:
        key.write_bytes(broken_key)
    expected = "saved_connection_key_missing" if broken_key is None else "saved_connection_corrupt"
    for operation in (lambda: store.load_draft("alpha", "new"),
                      lambda: store.save_draft("beta", "new", draft(), 1),
                      lambda: save_profile(store)):
        with pytest.raises(LightGraphError, match=f"^{expected}$"):
            operation()
    assert store.delete_draft("alpha", "new", 2) == 2
    assert store.load_draft_revision("alpha", "new") == 2
    store.save_draft("beta", "new", draft(), 3)
    assert store.load_draft("beta", "new")["revision"] == 3


def test_profile_prevents_key_rotation_after_draft_erasure(tmp_path):
    store = SavedConnections(tmp_path)
    save_profile(store)
    store.save_draft("alpha", "new", draft(), 1)
    (tmp_path / ".connection-key").unlink()
    store.delete_draft("alpha", "new", 2)
    with pytest.raises(LightGraphError, match="^saved_connection_key_missing$"):
        store.save_draft("beta", "new", draft(), 3)
    store.delete("alpha", "db")
    store.save_draft("beta", "new", draft(), 3)
    assert store.load_draft("beta", "new")["revision"] == 3


def test_corrupt_payload_is_not_exposed_and_can_be_deleted(tmp_path):
    store = SavedConnections(tmp_path)
    store.save_draft("alpha", "new", draft(), 1)
    path = tmp_path / "alpha/.connection-drafts/new.enc"
    path.write_bytes(b"SECRET_BROKEN_CIPHERTEXT")
    with pytest.raises(LightGraphError, match="^saved_connection_corrupt$") as captured:
        store.load_draft("alpha", "new")
    assert "SECRET" not in str(captured.value)
    store.delete_draft("alpha", "new", 2)
    assert not path.exists()


def test_invalid_decrypted_data_is_rejected(tmp_path):
    store = SavedConnections(tmp_path)
    store.save_draft("alpha", "new", draft(), 1)
    cipher = Fernet((tmp_path / ".connection-key").read_bytes())
    path = tmp_path / "alpha/.connection-drafts/new.enc"
    value = json.loads(cipher.decrypt(path.read_bytes()))
    value["revision"] = "not-an-integer"
    path.write_bytes(cipher.encrypt(json.dumps(value).encode()))
    with pytest.raises(LightGraphError, match="^saved_connection_corrupt$"):
        store.load_draft("alpha", "new")


def test_failed_atomic_save_preserves_old_revision(tmp_path, monkeypatch):
    store = SavedConnections(tmp_path)
    store.save_draft("alpha", "new", draft(), 1)
    def fail(*args):
        raise OSError("SECRET_LOCATION")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(LightGraphError, match="^saved_connection_storage_failed$"):
        store.save_draft("alpha", "new", draft(password="replacement"), 2)
    assert store.load_draft("alpha", "new")["revision"] == 1
    assert not list((tmp_path / "alpha/.connection-drafts").glob(".connection-*"))


def test_empty_load_does_not_create_key_or_workspace(tmp_path):
    root = tmp_path / "workspace"
    assert SavedConnections(root).load_draft("alpha", "new") is None
    assert SavedConnections(root).load_draft_revision("alpha", "new") == 0
    assert not root.exists()


@pytest.mark.skipif(os.name != "posix", reason="Windows uses inherited workspace ACLs")
def test_draft_and_tombstone_have_private_permissions(tmp_path):
    store = SavedConnections(tmp_path)
    store.save_draft("alpha", "new", draft(), 1)
    path = tmp_path / "alpha/.connection-drafts"
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert stat.S_IMODE((path / "new.enc").stat().st_mode) == 0o600
    store.delete_draft("alpha", "new", 2)
    assert stat.S_IMODE((path / "new.rev").stat().st_mode) == 0o600


@pytest.mark.parametrize("filename", ["new.enc", "new.rev"])
def test_unsafe_draft_paths_are_never_erased(tmp_path, filename):
    store = SavedConnections(tmp_path)
    store.save_draft("alpha", "new", draft(), 1)
    path = tmp_path / "alpha/.connection-drafts" / filename
    target = tmp_path / "untouched"
    target.write_bytes(b"untouched")
    path.unlink(missing_ok=True)
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("Symlinks require Windows developer mode or privileges")
    with pytest.raises(LightGraphError, match="^saved_connection_path_unsafe$"):
        store.delete_draft("alpha", "new", 2)
    assert target.read_bytes() == b"untouched"
