"""Model views are bounded; retained tool values remain exact and scoped."""
import json
import pytest

from lightgraph.construction.result_materials import ResultMaterialStore, token_size, result_token_limit
from lightgraph.construction.tools import ConstructionTools
from lightgraph.errors import LightGraphError
from lightgraph.tests.test_construction_tools import config_for


def store(tmp_path, **kwargs):
    return ResultMaterialStore(tmp_path, "context", "access", **kwargs)


def restore(materials, ref, section="", budget=None):
    page, chunks, first = 0, [], None
    while True:
        current = materials.read(ref, page, section, access="access", query_budget=budget or {})
        assert token_size(current) <= materials.token_limit
        assert current["requires_database"] is False
        if first is None:
            first = current
        assert current["material_revision"] == first["material_revision"]
        assert current["section_digest"] == first["section_digest"]
        assert current["character_start"] == sum(map(len, chunks))
        chunks.append(current["content"])
        if current["next_page"] is None:
            return json.loads("".join(chunks)), first
        page = current["next_page"]


@pytest.mark.parametrize("value", ["中文说明" * 8000, 'quote\\\"\n' * 5000])
def test_oversized_single_cell_is_exact_and_every_wrapper_is_token_bounded(tmp_path, value):
    materials = store(tmp_path)
    original = {"status": "ok", "coverage": {"result_complete": False, "truncated": True},
                "evidence_ref": "e19", "data": {"rows": [[value]], "row_count": 1}}
    view = materials.bound(original, "inspect_source")
    assert token_size(view) <= materials.token_limit
    assert view["evidence_ref"] == "e19"
    assert view["coverage"] == original["coverage"]
    assert view["result_presentation"]["source_complete"] is False
    assert view["result_presentation"]["presentation_complete"] is False
    restored, first = restore(materials, view["result_ref"], "/data/rows/0/0")
    assert restored == value
    assert first["source_complete"] is False and first["source_evidence_ref"] == "e19"
    assert first["pages"] > 1
    assert restore(materials, view["result_ref"])[0] == original
    materials.close()


def test_summary_retains_failure_controls_and_complete_tool_result(tmp_path):
    materials = store(tmp_path)
    result = {"status": "error", "code": "query_budget_exhausted", "retryable": False,
        "queries_stopped": True, "finalization_model_calls": 2,
        "query_budget": {"queries": 5, "remaining_queries": 0},
        "diagnostics": [{"code": "query_budget_exhausted", "message": "不可继续" * 100} for _ in range(200)]}
    summary = materials.bound(result, "validate_graph_draft")
    for key in ("status", "code", "retryable", "queries_stopped", "finalization_model_calls", "query_budget"):
        assert summary[key] == result[key]
    assert token_size(summary) <= materials.token_limit
    assert summary["result_presentation"]["stored_complete_tool_result"]
    restored, first = restore(materials, summary["result_ref"])
    assert restored == result and first["source_status"] == "error"
    assert first["source_code"] == "query_budget_exhausted"
    materials.close()


def test_selected_rows_and_escaped_object_keys_need_no_other_pages(tmp_path):
    materials = store(tmp_path)
    original = {"status": "ok", "data": {"rows": [{"id": i, "name": "资料" * 200} for i in range(200)]},
                "a/b~c": {"chosen": "准确"}}
    view = materials.bound(original, "inspect_source")
    selected, info = restore(materials, view["result_ref"], "/data/rows/123/id")
    assert selected == 123 and info["pages"] == 1
    assert restore(materials, view["result_ref"], "/a~1b~0c/chosen")[0] == "准确"
    for section in ("../secret", "/data/rows/01", "/a~2b", "/missing"):
        with pytest.raises(LightGraphError):
            materials.read(view["result_ref"], 0, section, access="access", query_budget={})
    materials.close()


def test_reference_is_deterministic_private_retained_and_session_bound(tmp_path):
    materials = store(tmp_path)
    result = {"status": "ok", "tables": [{"columns": "说明" * 10000}]}
    view = materials.bound(result, "describe_sources")
    assert view["result_ref"] == materials.bound(result, "describe_sources")["result_ref"]
    files = list(materials.root.glob("*.json"))
    assert len(files) == 1
    assert files[0].stat().st_mode & 0o777 == 0o600
    assert materials.root.stat().st_mode & 0o777 == 0o700
    other = store(tmp_path)
    with pytest.raises(LightGraphError, match="unknown_result_material"):
        other.read(view["result_ref"], 0, "", access="access", query_budget={})
    with pytest.raises(LightGraphError, match="result_material_access_changed"):
        materials.read(view["result_ref"], 0, "", access="changed", query_budget={})
    for invalid in ("result:../manifest.yaml", "result:" + "a" * 64):
        with pytest.raises(LightGraphError, match="unknown_result_material"):
            materials.read(invalid, 0, "", access="access", query_budget={})
    materials.close()
    other.close()
    assert files[0].exists()
    closed = materials.bound(result, "describe_sources")
    assert closed["status"] == "ok" and closed["presentation_error"] == "result_material_write_failed"
    assert "result_ref" not in closed
    with pytest.raises(LightGraphError, match="unknown_result_material"):
        materials.read(view["result_ref"], 0, "", access="access", query_budget={})


def test_integrity_and_symlink_replacement_do_not_return_tampered_content(tmp_path):
    materials = store(tmp_path)
    ref = materials.bound({"status": "ok", "data": "长" * 10000}, "read_workspace")["result_ref"]
    path = next(materials.root.glob("*.json"))
    value = json.loads(path.read_text())
    value["result"]["data"] = "tampered"
    path.write_text(json.dumps(value))
    with pytest.raises(LightGraphError, match="result_material_integrity_failed"):
        materials.read(ref, 0, "", access="access", query_budget={})
    path.unlink()
    target = tmp_path / "private.json"
    target.write_text(json.dumps(value))
    path.symlink_to(target)
    with pytest.raises(LightGraphError, match="result_material_unavailable"):
        materials.read(ref, 0, "", access="access", query_budget={})
    materials.close()


def test_write_failure_preserves_executed_mutation_status(tmp_path, monkeypatch):
    materials = store(tmp_path)
    def fail(*_args):
        raise OSError("disk unavailable")
    monkeypatch.setattr(materials, "_save", fail)
    original = {"status": "ok", "draft_id": "saved", "state": "candidate", "data": "长" * 10000}
    view = materials.bound(original, "save_graph_draft")
    assert view["status"] == "ok" and view["draft_id"] == "saved"
    assert view["presentation_error"] == "result_material_write_failed"
    assert not view["result_presentation"]["stored_complete_tool_result"]
    assert "result_ref" not in view
    assert token_size(view) <= materials.token_limit


def test_small_configured_budget_is_reported_without_silent_minimum(tmp_path):
    config = config_for(tmp_path)
    config.model.max_prompt_tokens = 80
    assert result_token_limit(config.resolved_model) == 10
    materials = store(tmp_path, token_limit=10)
    view = materials.bound({"status": "ok", "data": "长" * 1000}, "read_workspace")
    assert view["status"] == "ok"
    assert view["result_presentation"]["estimated_token_limit"] == 10
    assert view["result_presentation"]["presentation_error"] == "result_budget_too_small"
    page = materials.read(view["result_ref"], 0, "", access="access", query_budget={})
    assert page["presentation_error"] == "result_budget_too_small"
    assert not page["content"]
    materials.close()


def test_pages_stay_consistent_as_current_budget_changes(tmp_path):
    materials = store(tmp_path)
    result = {"status": "ok", "data": "待查" * 30000}
    ref = materials.bound(result, "inspect_source")["result_ref"]
    first = materials.read(ref, 0, "/data", access="access", query_budget={"remaining_seconds": 1000.231})
    second = materials.read(ref, 1, "/data", access="access", query_budget={"remaining_seconds": 0.1})
    assert first["character_end"] == second["character_start"]
    assert first["pages"] == second["pages"]
    assert second["query_budget"] == {"remaining_seconds": 0.1}
    assert token_size(first) <= materials.token_limit and token_size(second) <= materials.token_limit
    materials.close()


def test_construction_retrieval_does_not_query_or_issue_evidence(tmp_path, monkeypatch):
    tools = ConstructionTools(config_for(tmp_path), tmp_path / "work")
    try:
        result = {"status": "ok", "coverage": {"result_complete": False, "truncated": True},
                  "data": {"rows": [["巨型字段" * 5000]]}, "evidence_ref": "existing"}
        calls = []
        monkeypatch.setattr(tools, "inspect_source", lambda **kwargs: calls.append(kwargs) or result.copy())
        view = tools.bounded_call("inspect_source", table="test", operation="test")
        query_count = tools.query_budget.snapshot()["queries"]
        evidence = tools.workspace._manifest()["evidence"].copy()
        page = tools.bounded_call("read_material", name=view["result_ref"], section="/data/rows/0/0")
        assert len(calls) == 1
        assert tools.query_budget.snapshot()["queries"] == query_count
        assert tools.workspace._manifest()["evidence"] == evidence
        assert page["result_ref"] == view["result_ref"]
        assert "result_presentation" not in page
        assert token_size(page) <= tools.result_materials.token_limit
        assert page["source_complete"] is False
        assert len(list(tools.result_materials.root.glob("*.json"))) == 1
        # Changing fact access on the same instance cannot recover earlier rows.
        tools.config.sources[0].allow_fact_data = False
        denied = tools.call("read_material", name=view["result_ref"])
        assert denied["code"] == "result_material_access_changed"
    finally:
        tools.close()


def test_internal_call_keeps_complete_results_and_model_catalog_is_recoverable(tmp_path, monkeypatch):
    tools = ConstructionTools(config_for(tmp_path), tmp_path / "work")
    try:
        catalog = {"status": "ok", "tables": [{"name": f"table{i}", "columns": ["列" * 200] * 20} for i in range(60)]}
        monkeypatch.setattr(tools, "describe_sources", lambda: catalog.copy())
        full = tools.call("describe_sources")
        bounded = tools.bounded_call("describe_sources")
        assert full["tables"] == catalog["tables"]
        assert len(bounded.get("tables", [])) < len(full["tables"])
        assert token_size(bounded) <= tools.result_materials.token_limit
        page = tools.bounded_call("read_material", name=bounded["result_ref"], section="/tables/42/name")
        assert json.loads(page["content"]) == "table42"
    finally:
        tools.close()


def test_windows_style_capabilities_work_without_posix_constants(tmp_path, monkeypatch):
    from lightgraph.construction import result_materials as module
    with monkeypatch.context() as patch:
        patch.delattr(module.os, "O_DIRECTORY", raising=False)
        patch.delattr(module.os, "O_NOFOLLOW", raising=False)
        materials = store(tmp_path)
        assert not materials._use_dir_fd
        original = {"status": "ok", "data": "中文" * 4000}
        ref = materials.bound(original, "describe_sources")["result_ref"]
        assert restore(materials, ref)[0] == original
        materials.close()
    assert len(list(materials.root.glob("*.json"))) == 1


def test_windows_style_reader_rejects_redirected_files_and_directories(tmp_path):
    materials = store(tmp_path)
    materials._use_dir_fd = False
    ref = materials.bound({"status": "ok", "data": "长" * 10000}, "read_workspace")["result_ref"]
    path = next(materials.root.glob("*.json"))
    target = tmp_path / "foreign.json"
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises(LightGraphError, match="result_material_unavailable"):
        materials.read(ref, 0, "", access="access", query_budget={})
    materials.close()


def test_small_usable_window_includes_wrapper_and_live_budget(tmp_path, monkeypatch):
    config = config_for(tmp_path)
    config.model.working_context_tokens = 8192
    tools = ConstructionTools(config, tmp_path / "work")
    try:
        assert tools.result_materials.token_limit == 1024
        monkeypatch.setattr(tools, "describe_sources", lambda: {"status": "ok", "tables": ["中文" * 10000]})
        view = tools.bounded_call("describe_sources")
        assert "presentation_error" not in view["result_presentation"]
        assert token_size(view) <= 1024
        page = tools.bounded_call("read_material", name=view["result_ref"], section="/tables/0")
        assert "presentation_error" not in page
        assert token_size(page) <= 1024
        assert page["content"] and page["next_page"] == 1
    finally:
        tools.close()


def test_offloaded_document_does_not_claim_model_read_the_stored_body(tmp_path, monkeypatch):
    tools = ConstructionTools(config_for(tmp_path), tmp_path / "work")
    try:
        original = {"status": "ok", "section_fully_read": True, "evidence_refs": ["doc@1:section"],
            "section": {"text": "完整原文" * 5000, "read_state": "complete"}, "next_page": None}
        monkeypatch.setattr(tools, "read_material", lambda **kwargs: original)
        view = tools.bounded_call("read_material", name="document:doc")
        assert view["result_presentation"]["presentation_complete"] is False
        assert view["result_presentation"]["source_complete"] is None
        assert "do not mean omitted content has been read" in view["result_presentation"]["retrieval"]
    finally:
        tools.close()
