"""Document manifests bind client-selected files or registered assets in a single revision."""
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
import yaml

from lightgraph.documents.core import load_document
from lightgraph.web.app import create_app

BASE = "/api/projects/p"


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "workspace", frontend_dir=tmp_path / "none")) as http:
        assert http.post("/api/projects", json={"name": "Documents", "project_id": "p"}).status_code == 201
        yield http


def tag(client):
    return client.get(BASE + "/config").headers["etag"]


def upload(client, name, body):
    r = client.post(BASE + "/files", files={"file": (name, body)}, headers={"If-Match": tag(client)})
    assert r.status_code == 201, r.text
    return r.json()


def submit(client, entries, files=(), *, preview=False, revision=None):
    payload = [("manifest", ("documents.yaml", yaml.safe_dump({"documents": entries}, allow_unicode=True).encode()))]
    payload.extend(("files", (path, body)) for path, body in files)
    return client.post(BASE + "/documents/manifest" + ("/paths" if preview else ""), files=payload,
                       headers={"If-Match": revision or tag(client)})


def test_manifest_registers_nested_paths_and_rebinds_without_uploads(client):
    upload(client, "a.csv", b"x\n1\n")
    upload(client, "b.csv", b"x\n2\n")
    guide = upload(client, "guide.md", b"# Global guide")
    config = client.get(BASE + "/config").json()
    ids = {s["table_name"]: s["id"] for s in config["sources"]}
    paths = ["one/guide.md", "two/guide.md", guide["relative_path"]]
    entries = [{"path": paths[0], "tables": ["a", "public.b"]},
               {"path": paths[1], "type": "analysis_knowledge"},
               {"path": paths[2], "type": "analysis_knowledge"}]
    initial_tag = tag(client)
    preview = submit(client, entries, preview=True)
    assert preview.status_code == 200
    assert preview.json() == {"paths": paths, "missing_paths": paths[:2]}
    assert tag(client) == initial_tag
    response = submit(client, entries, [(paths[0], "# 说明".encode()), (paths[1], b"# Reference")], revision=initial_tag)
    assert response.status_code == 200, response.text
    files = response.json()["files"]
    assert response.json()["imported"] == 3
    assert files[2]["file_id"] == guide["file_id"]
    assert [f["manifest_path"] for f in files[:2]] == paths[:2]
    assert len({f["file_id"] for f in files}) == 3
    workspace = client.app.state.workspace
    doc = load_document(workspace.file_path("p", files[0]["file_id"]))
    assert doc.scope.tables == [f"{ids['a']}.public.a", f"{ids['b']}.public.b"]
    assert doc.purpose == "data_description"
    assert files[1]["scope"]["tables"] == []
    assert set(client.get(BASE + "/config").json()["documents"]) == {f["relative_path"] for f in files}
    assert submit(client, entries, preview=True).json()["missing_paths"] == []
    repeated = submit(client, entries)
    assert repeated.headers["etag"] == response.headers["etag"]  # identical import does not dirty configuration
    assert [f["file_id"] for f in repeated.json()["files"]] == [f["file_id"] for f in files]
    entries[0]["tables"] = ["b"]
    assert submit(client, entries).json()["files"][0]["scope"]["tables"] == [f"{ids['b']}.public.b"]
    assert submit(client, entries, revision=initial_tag).status_code == 409


@pytest.mark.parametrize(("path", "field"), [
    ("../secret.md", "path"), ("/tmp/secret.md", "path"), ("D:\\private\\note.md", "path"),
    ("docs//note.md", "path"), ("http://localhost/a.md", "path"), ("not-a-document.yaml", "path"),
])
def test_paths_reject_nonlocal_or_nonmarkdown_without_reading_host_files(client, path, field):
    response = submit(client, [{"path": path, "type": "analysis_knowledge"}], preview=True)
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "invalid_document_manifest"
    assert error["diagnostics"][0]["path"] == f"documents[0].{field}"


def test_batch_validation_keeps_prior_bindings_and_reports_selector_position(client):
    for _ in range(2):
        upload(client, "items.csv", b"id\n0\n")
    existing = upload(client, "known.md", b"# Known")
    initial_config = client.get(BASE + "/config").json()
    workspace = client.app.state.workspace
    record = workspace.get("p")
    old = workspace.file_path("p", existing["file_id"]).read_bytes()
    entries = [{"path": "known.md", "type": "analysis_knowledge"},
               {"path": "new.md", "tables": ["items"]}]
    for bad in ("items", "missing"):
        entries[1]["tables"] = [bad]
        r = submit(client, entries, [("new.md", b"# New")])
        assert r.status_code == 400, r.text
        assert r.json()["error"]["diagnostics"][0]["path"] == "documents[1].tables[0]"
        assert workspace.get("p") == record
        assert workspace.get_config("p") == initial_config
        assert workspace.file_path("p", existing["file_id"]).read_bytes() == old
    # Same basename does not authorize reading another path or choosing an existing duplicate.
    upload(client, "known.md", b"another")
    assert submit(client, entries[:1], preview=True).status_code == 400
    r = submit(client, [{"path": "docs/known.md", "type": "analysis_knowledge"}])
    assert r.json()["error"]["diagnostics"][0]["path"] == "documents[0].path"


def test_failure_after_multiple_writes_rolls_back_and_active_run_prevents_import(client, monkeypatch):
    existing = upload(client, "x.md", b"#x")
    workspace = client.app.state.workspace
    old = workspace.file_path("p", existing["file_id"]).read_bytes()
    config = workspace.get_config("p")
    entries = [{"path": "x.md", "type": "analysis_knowledge"}, {"path": "y.md", "type": "analysis_knowledge"}]
    config_path = workspace.project_path("p") / "config.yaml"
    atomic = workspace._atomic
    failed = False
    def fail_once(path, data):
        nonlocal failed
        if Path(path) == config_path and not failed:
            failed = True
            raise OSError("write failed")
        atomic(path, data)
    monkeypatch.setattr(workspace, "_atomic", fail_once)
    assert submit(client, entries, [("y.md", b"# Test")]).status_code == 500
    assert failed
    assert workspace.get_config("p") == config
    assert [f["file_id"] for f in workspace.get("p")["files"]] == [existing["file_id"]]
    assert list((config_path.parent / "files").iterdir()) == [workspace.file_path("p", existing["file_id"])]
    assert workspace.file_path("p", existing["file_id"]).read_bytes() == old
    monkeypatch.setattr(client.app.state.manager, "active", lambda pid: True)
    assert submit(client, entries, [("y.md", b"# Test")]).status_code == 409


def test_explicit_global_and_existing_body_replacement_guard(client):
    guide = upload(client, "guide.md", b"keep this body")
    entries = [{"path": "guide.md", "type": "mixed", "tables": []}]
    r = submit(client, entries)
    assert r.status_code == 200, r.text
    assert r.json()["files"][0]["file_id"] == guide["file_id"]
    assert r.json()["files"][0]["scope"]["tables"] == []
    before = tag(client)
    r = submit(client, entries, [("guide.md", b"another body")])
    assert r.status_code == 400
    assert r.json()["error"]["diagnostics"][0]["path"] == "documents[0].path"
    assert tag(client) == before
    assert client.get(BASE + f"/files/{guide['file_id']}/content").json()["content"].endswith("keep this body")


def test_preserves_columns_and_section_bounds_and_refuses_incompatible_rebind(client):
    source = upload(client, "items.csv", b"id,label\n1,abc\n")
    upload(client, "other.csv", b"id,other\n2,other\n")
    assert client.post(BASE + "/metadata/refresh").status_code == 200
    key = f"s_{source['file_id'][2:]}.public.items"
    meta = {"purpose": "data_description", "scope": {"tables": [key], "columns": ["label"]},
            "sections": {"Notes": {"scope": {"tables": [key], "columns": ["label"]}}}}
    body = ("---\n" + yaml.safe_dump(meta) + "---\n# Notes\nDocumentation").encode()
    file = upload(client, "one.md", body)
    entries = [{"path": file["relative_path"], "type": "data_description", "tables": [key]}]
    r = submit(client, entries)
    assert r.status_code == 200, r.text
    workspace = client.app.state.workspace
    path = workspace.file_path("p", file["file_id"])
    doc = load_document(path)
    assert doc.scope.columns == ["label"]
    assert doc.sections[0].scope.columns == ["label"] and doc.sections[0].scope.tables == [key]
    before = path.read_bytes()
    entries[0]["tables"] = ["other"]
    r = submit(client, entries)
    assert r.status_code == 400, r.text
    assert path.read_bytes() == before


def test_removed_table_binding_can_be_reactivated_only_after_valid_new_assignment(client):
    old = upload(client, "first.csv", b"x\n1")
    upload(client, "second.csv", b"x\n2")
    entries = [{"path": "guide.md", "tables": ["first"]}]
    doc = submit(client, entries, [("guide.md", b"# Guide")]).json()["files"][0]
    session = client.post(BASE + "/session").json()["session_id"]
    removed = client.delete(BASE + f"/files/{old['file_id']}",
                            headers={"X-Workbench-Session": session, "If-Match": tag(client)})
    assert removed.status_code == 200, removed.text
    assert doc["file_id"] in removed.json()["inactive_document_ids"]
    assert submit(client, entries).status_code == 400
    assert doc["relative_path"] not in client.get(BASE + "/config").json()["documents"]
    entries[0]["tables"] = ["second"]
    result = submit(client, entries)
    assert result.status_code == 200, result.text
    record = result.json()["files"][0]
    assert record["inactive_reason"] is None and not record["inactive_targets"]
    assert doc["relative_path"] in client.get(BASE + "/config").json()["documents"]
