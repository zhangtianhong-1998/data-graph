"""Connection profiles persist encrypted configuration, never source data."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import stat

from cryptography.fernet import Fernet
import pytest

from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import SourceConfig
from lightgraph.web.database_access import ConnectionSpec
from lightgraph.web.saved_connections import SavedConnections


def source(**updates):
    return SourceConfig(id="gauss-1", kind="gauss_database", credential_mode="session",
                        credential_revision="revision-1").model_copy(update=updates)


def connection(**updates):
    data = {"kind": "gauss_database", "dsn": "jdbc:gaussdb://db.example:8000/业务库",
            "username": "用户", "password": "SENSITIVE_密 码@:/?#&'\\\n", "schema": "public",
            "sslmode": "prefer"}
    data.update(updates)
    return ConnectionSpec.model_validate(data)


DSN = "host=db.example port=8000 dbname=业务库 user=用户 password='SENSITIVE_PASSWORD' sslmode=prefer"


def test_restart_restores_exact_configuration_but_only_ciphertext_is_on_disk(tmp_path):
    root = tmp_path / "workspace"
    spec = connection()
    SavedConnections(root).save("alpha", source(), spec, DSN)
    loaded = SavedConnections(root).load("alpha", source())
    assert loaded == {"dsn": DSN, "connection": {"kind": "gauss_database",
        "dsn": spec.dsn.get_secret_value(), "username": "用户",
        "password": spec.password.get_secret_value(), "schema": "public", "sslmode": "prefer"}}
    for path in root.rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            for value in (DSN, spec.dsn.get_secret_value(), spec.password.get_secret_value(), "db.example", "用户"):
                assert value.encode() not in content
    assert list(root.rglob("*.enc")) == [root / "alpha" / ".connections" / "gauss-1.enc"]
    assert [p.name for p in root.rglob(".connection-*")] == [".connection-key"]


def test_fields_and_mysql_configuration_round_trip(tmp_path):
    original = ConnectionSpec(kind="mysql_database", host="db.example", port=3306,
                              database="database", username="u", password="a :@/?# 密码")
    src = source(kind="mysql_database")
    SavedConnections(tmp_path).save("alpha", src, original, "mysql://normalized")
    loaded = SavedConnections(tmp_path).load("alpha", src)
    assert loaded["connection"]["password"] == "a :@/?# 密码"
    assert loaded["connection"]["host"] == "db.example"
    assert "dsn" not in loaded["connection"] and "schema" not in loaded["connection"]


@pytest.mark.parametrize("changed", [
    {"credential_revision": "new-revision"}, {"credential_revision": None},
    {"credential_mode": "environment"}, {"kind": "mysql_database"},
])
def test_changed_source_cannot_reuse_old_profile(tmp_path, changed):
    store = SavedConnections(tmp_path)
    store.save("alpha", source(), connection(), DSN)
    assert store.load("alpha", source(**changed)) is None


def test_replacement_invalidates_prior_revision(tmp_path):
    store = SavedConnections(tmp_path)
    store.save("alpha", source(), connection(), DSN)
    updated = source(credential_revision="new-revision")
    store.save("alpha", updated, connection(password="new-password"), "new-dsn")
    assert SavedConnections(tmp_path).load("alpha", source()) is None
    assert SavedConnections(tmp_path).load("alpha", updated)["dsn"] == "new-dsn"


@pytest.mark.parametrize("pid,sid", [("beta", "gauss-1"), ("alpha", "different-source")])
def test_copying_ciphertext_cannot_change_binding(tmp_path, pid, sid):
    store = SavedConnections(tmp_path)
    store.save("alpha", source(), connection(), DSN)
    original = tmp_path / "alpha" / ".connections" / "gauss-1.enc"
    target = tmp_path / pid / ".connections" / (sid + ".enc")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(original.read_bytes())
    assert store.load(pid, source(id=sid)) is None


def test_no_key_is_created_for_empty_store(tmp_path):
    root = tmp_path / "workspace"
    store = SavedConnections(root)
    assert store.load("alpha", source()) is None
    store.delete("alpha", "gauss-1")
    assert not root.exists()


def test_delete_is_idempotent_and_does_not_remove_other_source(tmp_path):
    store = SavedConnections(tmp_path)
    store.save("alpha", source(), connection(), DSN)
    store.save("alpha", source(id="other"), connection(), DSN)
    store.delete("alpha", "gauss-1")
    store.delete("alpha", "gauss-1")
    assert store.load("alpha", source()) is None
    assert store.load("alpha", source(id="other"))["dsn"] == DSN


def test_missing_key_fails_closed_for_load_and_new_saves(tmp_path):
    store = SavedConnections(tmp_path)
    store.save("alpha", source(), connection(), DSN)
    key = tmp_path / ".connection-key"
    key.unlink()
    for operation in (lambda: store.load("alpha", source()),
                      lambda: store.save("beta", source(), connection(), DSN)):
        with pytest.raises(LightGraphError, match="^saved_connection_key_missing$"):
            operation()
    assert not key.exists()


@pytest.mark.parametrize("broken_key", [b"corrupt-key", b"corrupt-key" * 100])
def test_corrupt_key_replaced_only_after_all_profiles_are_deleted(tmp_path, broken_key):
    store = SavedConnections(tmp_path)
    store.save("alpha", source(), connection(), DSN)
    store.save("beta", source(), connection(), DSN)
    key = tmp_path / ".connection-key"
    key.write_bytes(broken_key)
    store.delete("alpha", source().id)
    with pytest.raises(LightGraphError, match="^saved_connection_corrupt$"):
        store.save("alpha", source(), connection(), DSN)
    assert key.read_bytes() == broken_key
    store.delete("beta", source().id)
    SavedConnections(tmp_path).save("alpha", source(), connection(), DSN)
    assert key.read_bytes() != broken_key
    assert SavedConnections(tmp_path).load("alpha", source())["dsn"] == DSN


def test_unsafe_key_not_replaced_even_when_no_profiles_remain(tmp_path):
    store = SavedConnections(tmp_path)
    store.save("alpha", source(), connection(), DSN)
    store.delete("alpha", source().id)
    key = tmp_path / ".connection-key"
    original = key.read_bytes()
    alias = tmp_path / "key-hardlink"
    os.link(key, alias)
    with pytest.raises(LightGraphError, match="^saved_connection_path_unsafe$"):
        store.save("alpha", source(), connection(), DSN)
    assert key.read_bytes() == original
    assert key.stat().st_nlink == 2


@pytest.mark.parametrize("filename", [".connection-key", "alpha/.connections/gauss-1.enc"])
def test_tampered_files_fail_without_secret_details(tmp_path, filename):
    store = SavedConnections(tmp_path)
    store.save("alpha", source(), connection(), DSN)
    (tmp_path / filename).write_bytes(b"SENSITIVE_BROKEN_PAYLOAD")
    with pytest.raises(LightGraphError, match="^saved_connection_corrupt$") as captured:
        store.load("alpha", source())
    assert captured.value.__suppress_context__
    assert captured.value.__cause__ is None


def test_invalid_decrypted_configuration_is_corrupt(tmp_path):
    store = SavedConnections(tmp_path)
    store.save("alpha", source(), connection(), DSN)
    cipher = Fernet((tmp_path / ".connection-key").read_bytes())
    profile = tmp_path / "alpha/.connections/gauss-1.enc"
    payload = json.loads(cipher.decrypt(profile.read_bytes()))
    payload["connection"] = {"dsn": "SENSITIVE_INVALID"}
    profile.write_bytes(cipher.encrypt(json.dumps(payload).encode()))
    with pytest.raises(LightGraphError, match="^saved_connection_corrupt$"):
        store.load("alpha", source())


@pytest.mark.skipif(os.name != "posix", reason="Windows uses inherited workspace ACLs")
def test_files_and_directories_use_owner_only_modes(tmp_path):
    root = tmp_path / "workspace"
    SavedConnections(root).save("alpha", source(), connection(), DSN)
    for path in (root, root / "alpha", root / "alpha/.connections"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    for path in (root / ".connection-key", root / "alpha/.connections/gauss-1.enc"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("relative", [".connection-key", "alpha", "alpha/.connections", "alpha/.connections/gauss-1.enc"])
def test_symlink_paths_are_rejected(tmp_path, relative):
    root = tmp_path / "workspace"
    store = SavedConnections(root)
    store.save("alpha", source(), connection(), DSN)
    path = root / relative
    moved = tmp_path / "moved"
    path.rename(moved)
    try:
        path.symlink_to(moved, target_is_directory=moved.is_dir())
    except OSError:
        pytest.skip("Creating symlinks requires Windows developer mode or privileges")
    with pytest.raises(LightGraphError, match="^saved_connection_path_unsafe$"):
        store.load("alpha", source())
    with pytest.raises(LightGraphError, match="^saved_connection_path_unsafe$"):
        store.save("alpha", source(), connection(), DSN)


def test_root_symlink_is_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "workspace"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires Windows developer mode or privileges")
    with pytest.raises(LightGraphError, match="^saved_connection_path_unsafe$"):
        SavedConnections(link)


@pytest.mark.parametrize("pid,sid", [("../escape", "safe"), ("safe", "../escape"),
                                     ("safe", "slash/name"), ("safe", "bad\\name"),
                                     ("safe", "x" * 129)])
def test_invalid_paths_are_rejected(tmp_path, pid, sid):
    with pytest.raises(LightGraphError, match="^saved_connection_invalid$"):
        SavedConnections(tmp_path).save(pid, source(id=sid), connection(), DSN)


def test_failed_atomic_publication_preserves_previous_profile(tmp_path, monkeypatch):
    store = SavedConnections(tmp_path)
    store.save("alpha", source(), connection(), DSN)
    def denied(*_args):
        raise PermissionError("SENSITIVE_DRIVER_PATH")
    monkeypatch.setattr(os, "replace", denied)
    with pytest.raises(LightGraphError, match="^saved_connection_storage_failed$"):
        store.save("alpha", source(), connection(password="changed"), "new-dsn")
    assert store.load("alpha", source())["dsn"] == DSN
    assert not list((tmp_path / "alpha/.connections").glob(".connection-*"))


def test_separate_store_instances_coordinate_key_creation(tmp_path):
    def save(index):
        SavedConnections(tmp_path).save("alpha", source(id=f"source-{index}"), connection(), f"dsn-{index}")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(save, range(16)))
    for index in range(16):
        assert SavedConnections(tmp_path).load("alpha", source(id=f"source-{index}"))["dsn"] == f"dsn-{index}"
