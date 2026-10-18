"""Rollback revalidates current source and dependency rules through the actual CLI."""
import json
from pathlib import Path

import pytest
import yaml

from lightgraph import cli
from lightgraph.contracts import ColumnSpec, TableSpec, RuleSpec, RunReport
from lightgraph.pipeline import run_pipeline
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore


def fixture(tmp_path):
    columns = ["tenant", "metric_code", "metric_name", "old_definition", "new_definition",
               "measure_code", "measure_name", "measure_alias"]
    table = TableSpec(source_id="s", name="items", columns=[ColumnSpec(name=c) for c in columns])
    rows = [{"tenant": "A", "metric_code": "K1", "metric_name": "收入", "old_definition": "SUM(sales)",
             "new_definition": "AVG(sales)", "measure_code": "M1", "measure_name": "累计金额", "measure_alias": "累计总额"}]
    metric = RuleSpec(rule_id="metrics", kind="column_group_entity", source={"source_id": "s", "table": "items"},
        scope={"namespaces": ["tenant"]}, identity={"key_columns": ["tenant", "metric_code"], "entity_kind": "Metric"},
        attributes={"single_valued": {"label": "metric_name", "definition": "old_definition"}})
    measure = RuleSpec(rule_id="measures", kind="column_group_entity", source={"source_id": "s", "table": "items"},
        scope={"namespaces": ["tenant"]}, identity={"key_columns": ["tenant", "measure_code"], "entity_kind": "Measure"},
        attributes={"single_valued": {"label": "measure_name"}})
    relation = RuleSpec(rule_id="metric_measure", kind="reference", source={"source_id": "s", "table": "items"},
        scope={"namespaces": ["tenant"]}, identity={"key_columns": ["tenant", "metric_code", "measure_code"], "entity_kind": "Metric"},
        relation={"source_rule_id": "metrics", "target_rule_id": "measures", "source_columns": ["tenant", "metric_code"],
                  "target_columns": ["tenant", "measure_code"], "relation": "uses_measure"},
        evidence={"semantic_basis": ["declared_dependency"]})
    config = RunConfig(sources=[], output_dir=str(tmp_path / "output"), scopes=["A", "public"])
    with MemorySource([table], {"public.items": rows}) as source:
        first = run_pipeline(config, [source], rules=[metric, measure, relation])
    assert first.status == "COMPLETED", first.issues
    store_path = tmp_path / "output" / "graph.sqlite"
    with GraphStore(store_path) as store:
        old_publication = next(h["publication_id"] for h in store.heads() if h["logical_rule_id"] == "metrics")
    changed = metric.model_copy(deep=True)
    changed.revision = 2
    changed.attributes.single_valued["definition"] = "new_definition"
    with MemorySource([table], {"public.items": rows}) as source:
        second = run_pipeline(config, [source], rules=[changed, measure, relation])
    assert second.status == "COMPLETED", second.issues
    return table, rows, config, store_path, old_publication, measure


def run_cli(tmp_path, monkeypatch, table, rows, config, capsys):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    monkeypatch.setattr(cli, "open_sources", lambda cfg, metadata_only=False:
                        [MemorySource([table], {"public.items": rows})])
    status = cli.main(["rollback", str(config_path), "metrics", "1"])
    return status, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("invalidate_dependents", [False, True])
def test_cli_can_rollback_stale_history_and_rebuild_related_rules(tmp_path, monkeypatch, capsys, invalidate_dependents):
    table, rows, config, path, old_pub, _ = fixture(tmp_path)
    with GraphStore(path) as store:
        old_payload = store.connection.execute("SELECT payload FROM rule WHERE rule_id='metrics' AND revision=1").fetchone()[0]
        if invalidate_dependents:
            store.invalidate_rule("metrics", "explicit_revalidation")
        else:
            store.connection.execute("UPDATE rule SET dependency_state='stale' WHERE rule_id='metrics' AND revision=1")
    # Rollback must execute the old definition against current values, not make
    # the old graph snapshot current again.
    rows[0]["old_definition"] = "SUM(current_sales)"
    status, report = run_cli(tmp_path, monkeypatch, table, rows, config, capsys)
    assert status == 0 and report["status"] == "COMPLETED", report
    assert set(report["rules_published"]) == {"metrics", "measures", "metric_measure"}
    with GraphStore(path) as store:
        graph = store.view({"A", "public"})
        metric = next(n for n in graph["nodes"] if n["kind"] == "Metric")
        measure = next(n for n in graph["nodes"] if n["kind"] == "Measure")
        assert metric["properties"]["definition"] == "SUM(current_sales)"
        assert any(e["relation"] == "uses_measure" and e["source"] == metric["id"] and e["target"] == measure["id"] for e in graph["edges"])
        assert max(r.revision for r in store.get_rules() if r.rule_id == "metrics") == 3
        assert store.connection.execute("SELECT payload FROM rule WHERE rule_id='metrics' AND revision=1").fetchone()[0] == old_payload
        historical = store.view({"A", "public"}, publication_id=old_pub)
        assert next(n for n in historical["nodes"] if n["kind"] == "Metric")["properties"]["definition"] == "SUM(sales)"
        assert store.connection.execute("SELECT COUNT(*) FROM rule_event WHERE event_type='rollback_requested'").fetchone()[0] == 1


def test_cli_rollback_preserves_config_override_and_fills_missing_dependencies(tmp_path, monkeypatch, capsys):
    table, rows, config, path, _, measure = fixture(tmp_path)
    override = measure.model_copy(deep=True)
    override.revision = 2
    override.attributes.single_valued["label"] = "measure_alias"
    rules_path = tmp_path / "configured.yaml"
    rules_path.write_text(yaml.safe_dump([override.model_dump(mode="json", by_alias=True)]), encoding="utf-8")
    config.rules = [str(rules_path)]
    status, report = run_cli(tmp_path, monkeypatch, table, rows, config, capsys)
    assert status == 0, report
    assert set(report["rules_published"]) == {"metrics", "measures", "metric_measure"}
    with GraphStore(path) as store:
        graph = store.view({"A", "public"})
        assert next(n for n in graph["nodes"] if n["kind"] == "Measure")["label"] == "累计总额"
        assert any(e["relation"] == "uses_measure" for e in graph["edges"])


def test_cli_rollback_rejects_incompatible_current_schema_without_changing_heads(tmp_path, monkeypatch, capsys):
    table, rows, config, path, _, _ = fixture(tmp_path)
    with GraphStore(path) as store:
        before = store.heads()
    table.columns = [c for c in table.columns if c.name != "old_definition"]
    rows[0].pop("old_definition")
    status, result = run_cli(tmp_path, monkeypatch, table, rows, config, capsys)
    assert status == 1 and result["code"] == "rollback_dependencies_incompatible"
    with GraphStore(path) as store:
        assert store.heads() == before


@pytest.mark.parametrize("size", [99, 100001])
def test_verify_rejects_size_before_starting(tmp_path, monkeypatch, capsys, size):
    import lightgraph.acceptance as acceptance
    monkeypatch.setattr(acceptance, "verify", lambda *args: pytest.fail("invalid size reached execution"))
    assert cli.main(["verify", "--output", str(tmp_path), "--rows", str(size)]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "sample_size_out_of_range"


def test_demo_failed_report_returns_failure_exit(tmp_path, monkeypatch, capsys):
    import lightgraph.pipeline as pipeline
    monkeypatch.setattr(pipeline, "run_pipeline", lambda *args, **kwargs: RunReport(run_id="failed", status="FAILED"))
    assert cli.main(["demo", "--output", str(tmp_path), "--rows", "100"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "FAILED"
