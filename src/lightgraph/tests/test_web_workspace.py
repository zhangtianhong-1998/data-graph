from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path

import pytest

from lightgraph.documents.core import load_documents
from lightgraph.errors import LightGraphError
from lightgraph.web.workspace import MAX_TEXT_BYTES, MAX_UPLOAD_BYTES, Workspace


@pytest.fixture
def workspace(tmp_path):
    store = Workspace(tmp_path / "workspace")
    store.create("试验 A", "project_a")
    store.create("试验 B", "project_b")
    return store


def test_project_isolation_restart_and_default_folders(workspace):
    a = workspace.add_file("project_a", "经营.csv", b"code,name\n2150,expense\n")
    b = workspace.add_file("project_b", "经营.csv", b"code,name\n2151,revenue\n")
    assert a["relative_path"] != b["relative_path"]
    with pytest.raises(LightGraphError, match="project_file_not_found"):
        workspace.file_path("project_b", a["file_id"])
    fresh = Workspace(workspace.root)
    assert fresh.get("project_a")["files"] == [a]
    assert fresh.file_path("project_a", a["file_id"]).read_bytes().endswith(b"expense\n")
    assert len(fresh.list_projects()) == 2
    assert fresh.rename("project_a", "新名字")["name"] == "新名字"
    for name in ("files", "output", "runs"):
        assert (fresh.project_path("project_a") / name).is_dir()
    config = fresh.get_config("project_a")
    assert config["goal"]["purpose"] == ""
    assert config["sources"][0]["encoding"] == "utf-8"
    assert config["sources"][0]["header"] is True
    assert config["sources"][0]["null_values"] == [""]
    assert config["sources"][0]["type_inference"] == "text"
    resolved = fresh.resolved_config("project_a")
    assert Path(resolved.sources[0].path) == fresh.file_path("project_a", a["file_id"])
    assert resolved.output_dir == str(fresh.root / "project_a" / "output")
    assert resolved.isolation_domain == "project_a"
    assert resolved.resolved_model.mode == "in_environment"
    assert resolved.max_working_bytes == 2048 * 1024**2
    assert resolved.max_returned_rows is None and resolved.max_returned_bytes is None


@pytest.mark.parametrize("project_id", ["../bad", "/tmp/bad", "x/y", "x\\y", "", ".", "..", "x" * 65])
def test_project_ids_reject_paths(workspace, project_id):
    with pytest.raises(LightGraphError, match="invalid_project_or_file_id"):
        workspace.create("Invalid", project_id)


@pytest.mark.parametrize("name", ["../x.md", "/tmp/x.csv", "x\\x.yaml", "x\x00.md", " .md", "x:one.md"])
def test_filename_cannot_escape(workspace, name):
    with pytest.raises(LightGraphError, match="invalid_upload_filename"):
        workspace.add_file("project_a", name, b"# hello")


def test_symlinked_project_root_file_and_output_rejected(workspace, tmp_path):
    target = tmp_path / "outside"
    target.mkdir()
    try:
        (workspace.root / "linked").symlink_to(target, target_is_directory=True)
    except OSError as error:
        if os.name == "nt" and getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows symlink creation requires Developer Mode or an authorized account")
        raise
    with pytest.raises(LightGraphError, match="symlink"):
        workspace.create("linked", "linked")
    with pytest.raises(LightGraphError, match="symlink"):
        Workspace(workspace.root / "linked")
    (workspace.root / "linked").unlink()
    doc = workspace.add_file("project_a", "guide.md", b"# Guide")
    path = workspace.file_path("project_a", doc["file_id"])
    path.unlink()
    path.symlink_to(tmp_path / "private.txt")
    with pytest.raises(LightGraphError, match="symlink"):
        workspace.file_path("project_a", doc["file_id"])
    path.unlink()
    path.write_bytes(b"# Guide")
    output = workspace.project_path("project_a") / "output"
    output.rmdir()
    output.symlink_to(target, target_is_directory=True)
    with pytest.raises(LightGraphError, match="symlink"):
        workspace.resolved_config("project_a")


def test_same_name_upload_never_overwrites_and_ambiguous_import_fails(workspace):
    one = workspace.add_file("project_a", "data.csv", b"a,b\n1,2\n")
    two = workspace.add_file("project_a", "data.csv", b"a,b\n3,4\n")
    assert one["file_id"] != two["file_id"]
    assert workspace.file_path("project_a", one["file_id"]).read_bytes() != workspace.file_path("project_a", two["file_id"]).read_bytes()
    config = workspace.get_config("project_a")
    config["sources"][0]["path"] = "data.csv"
    with pytest.raises(LightGraphError, match="unambiguous_project_file"):
        workspace.save_config("project_a", config)
    assert len(workspace.get_config("project_a")["sources"]) == 2


def test_config_paths_registered_and_forced_project_output(workspace):
    record = workspace.add_file("project_a", "data.csv", b"a,b\n1,2\n")
    config = workspace.get_config("project_a")
    config["sources"][0]["path"] = "data.csv"
    config.update(isolation_domain="other", output_dir="/tmp/outside")
    saved = workspace.save_config("project_a", config)
    assert saved["output_dir"] == "output"
    assert saved["isolation_domain"] == "project_a"
    assert saved["sources"][0]["path"] == record["relative_path"]
    for bad in ("/tmp/private.csv", "../project_b/data.csv", "files/not_registered.csv"):
        config["sources"][0]["path"] = bad
        with pytest.raises(LightGraphError, match="config_path"):
            workspace.save_config("project_a", config)
    for field in ("documents", "rules"):
        broken = workspace.get_config("project_a")
        broken[field] = ["/tmp/private.md"]
        with pytest.raises(LightGraphError, match="config_path"):
            workspace.save_config("project_a", broken)


def test_uploaded_config_imports_filename_and_agent_env(workspace):
    workspace.add_file("project_a", "data.csv", b"code,name\n2150,expense\n")
    agent = workspace.add_file("project_a", "agent.yaml", b"framework: agentscope\nllm:\n  mode: disabled\n  api_key_env: EXAMPLE_MODEL_KEY\n")
    config = workspace.add_file("project_a", "run.yaml", b"""sources:
  - id: source_a
    kind: csv_file
    path: data.csv
    encoding: utf-8
    delimiter: ','
    header: true
    null_values: ['']
    type_inference: text
agent_config: agent.yaml
goal:
  purpose: Build analysis graph
""")
    saved = workspace.import_config("project_a", config["file_id"])
    assert saved["agent_config"] == agent["relative_path"]
    assert "model" not in saved
    resolved = workspace.resolved_config("project_a")
    assert resolved.resolved_model.api_key_env == "EXAMPLE_MODEL_KEY"
    assert resolved.goal.purpose == "Build analysis graph"


def test_document_binding_is_read_by_real_loader_and_changes_revision(workspace):
    original = "---\ntitle: 字段说明\nauthor: 维护人员\n---\n# 使用说明\n\n此处为正文。\n"
    doc = workspace.add_file("project_a", "说明.md", original.encode())
    assert doc["scope"] == {"source_ids": [], "tables": [], "columns": []}
    assert doc["purpose"] == "data_description"
    assert doc["inactive_reason"] == "pending_assignment"
    assert workspace.resolved_config("project_a").documents == []
    before = load_documents([workspace.file_path("project_a", doc["file_id"])])[0]
    config = workspace.get_config("project_a")
    config["sources"] = [{"id": "sales", "kind": "gauss_database", "allowed_tables": ["public.items"]}]
    workspace.save_config("project_a", config)
    scope = {"source_ids": ["sales"], "tables": ["sales.public.items"], "columns": []}
    record = workspace.document_binding("project_a", doc["file_id"], "data_description", scope)
    after = load_documents(workspace.resolved_config("project_a").documents)[0]
    assert record["scope"] == scope
    assert after.scope.tables == ["sales.public.items"]
    assert after.purpose == "data_description"
    assert after.document_id == before.document_id
    assert after.revision != before.revision
    text = workspace.file_path("project_a", doc["file_id"]).read_text(encoding="utf-8")
    assert "author: 维护人员" in text
    assert text.endswith("# 使用说明\n\n此处为正文。\n")
    reopened = Workspace(workspace.root)
    assert reopened.get("project_a")["files"][0] == record


@pytest.mark.parametrize("text", ["x: &value [1]\ny: *value", "x: !!python/object:os.system {}", "x: 1\nx: 2"])
def test_yaml_alias_tag_duplicate_keys_rejected(workspace, text):
    with pytest.raises(LightGraphError, match="yaml"):
        workspace.add_file("project_a", "unsafe.yaml", text.encode())
    assert workspace.get("project_a")["files"] == []


def test_credentials_not_retained_or_echoed(workspace):
    secret = "SENTINEL_PRIVATE_MODEL_KEY"
    config = workspace.get_config("project_a")
    config["model"]["api_key"] = secret
    with pytest.raises(LightGraphError) as caught:
        workspace.save_config("project_a", config)
    assert secret not in str(caught.value)
    with pytest.raises(LightGraphError, match="credential_environment"):
        workspace.add_file("project_a", "agent.yaml", f"llm:\n  api_key: {secret}\n".encode())
    assert secret not in "".join(p.read_text(encoding="utf-8") for p in workspace.root.rglob("*.yaml"))
    # A rule's business attribute named password is not a connector credential.
    workspace.add_file("project_a", "rule.yaml", b"attributes:\n  single_valued:\n    password: source_column\n")


@pytest.mark.parametrize("url", ["https://user:secret@llm.example", "https://llm.example?api_key=secret", "https://llm.example#secret"])
def test_urls_cannot_smuggle_credentials_into_saved_config(workspace, url):
    config = workspace.get_config("project_a")
    config["model"]["base_url"] = url
    with pytest.raises(LightGraphError, match="credential_bearing_url"):
        workspace.save_config("project_a", config)


def test_failed_registration_restores_config_and_removes_upload(workspace, monkeypatch):
    before = workspace.get_config("project_a")
    def fail(project):
        raise LightGraphError("injected_write_failure")
    monkeypatch.setattr(workspace, "_save", fail)
    with pytest.raises(LightGraphError, match="injected_write_failure"):
        workspace.add_file("project_a", "data.csv", b"a,b\n1,2\n")
    assert workspace.get_config("project_a") == before
    assert workspace.get("project_a")["files"] == []
    assert list((workspace.root / "project_a" / "files").iterdir()) == []


def test_upload_limits_are_distinct_for_data_and_text(workspace):
    with pytest.raises(LightGraphError, match="upload_too_large"):
        workspace.add_file("project_a", "large.csv", b"x" * (MAX_UPLOAD_BYTES + 1))
    with pytest.raises(LightGraphError, match="upload_too_large"):
        workspace.add_file("project_a", "large.md", b"x" * (MAX_TEXT_BYTES + 1))


def test_project_lock_prevents_lost_concurrent_file_registrations(workspace):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda n: workspace.add_file("project_a", f"data{n}.csv", b"a,b\n1,2"), range(8)))
    assert len({file["file_id"] for file in results}) == 8
    assert len(workspace.get("project_a")["files"]) == 8
    assert len(workspace.get_config("project_a")["sources"]) == 8


def test_text_preview_rechecks_yaml_credentials_after_manual_edit(workspace):
    file = workspace.add_file("project_a", "agent.yaml", b"llm:\n  api_key_env: EXAMPLE_KEY\n")
    assert "EXAMPLE_KEY" in workspace.text_content("project_a", file["file_id"])
    path = workspace.file_path("project_a", file["file_id"])
    path.write_text("llm:\n  api_key: SECRET_FROM_MANUAL_EDIT\n")
    with pytest.raises(LightGraphError) as caught:
        workspace.text_content("project_a", file["file_id"])
    assert caught.value.code == "use_credential_environment_variable"
    assert "SECRET_FROM_MANUAL_EDIT" not in str(caught.value)


def test_text_preview_is_scoped_bounded_and_utf8(workspace):
    md = workspace.add_file("project_a", "说明.md", "# 项目说明".encode())
    assert workspace.text_content("project_a", md["file_id"]) == "# 项目说明"
    with pytest.raises(LightGraphError, match="project_file_not_found"):
        workspace.text_content("project_b", md["file_id"])
    csv = workspace.add_file("project_a", "data.csv", b"a,b\n1,2")
    with pytest.raises(LightGraphError, match="text_preview_unavailable"):
        workspace.text_content("project_a", csv["file_id"])
    path = workspace.file_path("project_a", md["file_id"])
    path.write_bytes(b"\xff\x00")
    with pytest.raises(LightGraphError, match="text_encoding_invalid"):
        workspace.text_content("project_a", md["file_id"])
    path.write_bytes(b"x" * (MAX_TEXT_BYTES + 1))
    with pytest.raises(LightGraphError, match="file_too_large"):
        workspace.text_content("project_a", md["file_id"])


def test_get_config_rejects_secret_from_manually_edited_project_config(workspace):
    path = workspace.project_path("project_a") / "config.yaml"
    path.write_text("sources: []\nmodel:\n  api_key: SECRET_FROM_DISK\n")
    with pytest.raises(LightGraphError) as caught:
        workspace.get_config("project_a")
    assert caught.value.code == "use_credential_environment_variable"
    assert "SECRET_FROM_DISK" not in str(caught.value)


def test_filename_bindings_reconcile_in_both_upload_orders_and_manual_selection_wins(workspace):
    pid = "project_a"
    doc = workspace.add_file(pid, "records.markdown", b"# Dictionary")
    config = workspace.get_config(pid)
    config["documents"] = [doc["relative_path"]]
    assert workspace.save_config(pid, config)["documents"] == []

    first = workspace.add_file(pid, "records.csv", b"id\n01\n")
    expected = f"s_{first['file_id'][2:]}.public.records"
    resolved = load_documents(workspace.resolved_config(pid).documents)
    assert len(resolved) == 1 and resolved[0].scope.tables == [expected]
    assert resolved[0].purpose == "data_description"

    second = workspace.add_file(pid, "records.csv", b"id\n02\n")
    assert workspace.resolved_config(pid).documents == []
    def current():
        return next(f for f in workspace.get(pid)["files"] if f["file_id"] == doc["file_id"])
    assert current()["inactive_reason"] == "pending_assignment"
    for selector, error in [("records", "ambiguous_document_table_binding"),
                            ("missing.public.records", "invalid_document_table_binding")]:
        with pytest.raises(LightGraphError, match=error):
            workspace.document_binding(pid, doc["file_id"], "data_description", {"tables": [selector]})
        assert current()["inactive_reason"] == "pending_assignment"
    scope = {"tables": [expected, f"s_{second['file_id'][2:]}.public.records"]}
    workspace.document_binding(pid, doc["file_id"], "data_description", scope)
    workspace.add_file(pid, "records.csv", b"id\n03\n")
    assert current()["binding_origin"] == "explicit"
    assert load_documents(workspace.resolved_config(pid).documents)[0].scope.tables == scope["tables"]


def test_filename_match_uses_exact_visible_sheet_and_table_names(workspace):
    from io import BytesIO
    from openpyxl import Workbook
    pid = "project_a"
    book = Workbook()
    book.active.title = "object.v2"
    book.active.append(["key"])
    book.create_sheet("hidden").sheet_state = "hidden"
    book["hidden"].append(["key"])
    book.create_sheet("empty")
    stream = BytesIO()
    book.save(stream)
    workbook = workspace.add_file(pid, "inventory.xlsx", stream.getvalue())
    source_id = f"s_{workbook['file_id'][2:]}"
    matching = workspace.add_file(pid, "object.v2.MD", b"# Valid")
    assert matching["scope"]["tables"] == [f"{source_id}.public.object.v2"]
    for name in ("OBJECT.V2.md", "object.md", "hidden.md", "empty.md", "inventory.md"):
        file = workspace.add_file(pid, name, b"# Pending")
        assert file["inactive_reason"] == "pending_assignment"
    assert workspace.get_config(pid)["documents"] == [matching["relative_path"]]


def test_explicit_and_legacy_bindings_survive_new_homonymous_source(workspace):
    pid = "project_a"
    file = workspace.add_file(pid, "archive.md", b"---\npurpose: analysis_knowledge\n---\n# Guide")
    project = workspace.get(pid)
    del project["files"][0]["binding_origin"]
    workspace._save(project)  # A project persisted before per-file binding provenance.
    workspace.add_file(pid, "archive.csv", b"code\nX\n")
    [doc] = load_documents(workspace.resolved_config(pid).documents)
    assert doc.purpose == "analysis_knowledge" and not doc.scope.tables
    assert workspace.get_config(pid)["documents"] == [file["relative_path"]]


def test_reverting_temporary_source_connection_restores_automatic_assignment(workspace):
    pid = "project_a"
    workspace.add_file(pid, "items.md", b"# Dictionary")
    previous = workspace.get_config(pid)
    source = {"id": "temp", "kind": "gauss_database", "allowed_tables": ["public.items"]}
    connected = workspace.save_config(pid, {**previous, "sources": [source]})
    assert connected["documents"]
    assert workspace.save_config(pid, previous)["documents"] == []
    doc = workspace.get(pid)["files"][0]
    assert doc["inactive_reason"] == "pending_assignment" and doc["binding_origin"] == "filename"
    assert workspace.save_config(pid, {**previous, "sources": [source]})["documents"] == connected["documents"]


def test_save_valid_replacement_can_repair_broken_existing_config(workspace):
    pid = "project_a"
    missing = workspace.add_file(pid, "missing.csv", b"id\n1\n")
    valid = workspace.get_config(pid)
    valid["sources"] = []
    workspace.file_path(pid, missing["file_id"]).unlink()
    assert workspace.save_config(pid, valid)["sources"] == []
    config_path = workspace.project_path(pid) / "config.yaml"
    config_path.write_bytes(b"invalid: [")
    assert workspace.save_config(pid, valid)["sources"] == []
    config_path.unlink()
    assert workspace.save_config(pid, valid)["sources"] == []


def test_literal_dotted_name_cannot_collide_with_an_automatic_qualified_selector(workspace):
    pid = "project_a"
    config = workspace.get_config(pid)
    config["sources"] = [{"id": "db", "kind": "mysql_database", "allowed_tables": ["s.object"]}]
    workspace.save_config(pid, config)
    workspace.add_file(pid, "db.s.object.csv", b"key\nA\n")
    file = workspace.add_file(pid, "object.md", b"# Dictionary")
    assert file["inactive_reason"] == "pending_assignment"
    assert not workspace.resolved_config(pid).documents
