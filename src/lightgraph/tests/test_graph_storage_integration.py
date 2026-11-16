"""Exercise storage through publication, DAG merge and authorized web readers."""
from copy import deepcopy
import json

import pytest

from lightgraph.construction import graph_storage, workspace
from lightgraph.construction.access import access_fingerprint
from lightgraph.construction.merger import _shard_bucket, write_sharded_graph
from lightgraph.construction.workflow import load_unit_contributor, publish_merged_contributors
from lightgraph.construction.workspace import GraphWorkspace, parse_graph_draft, read_internal_yaml, write_yaml
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import RunConfig
from lightgraph.web.construction_graph import load_graph
from lightgraph.web.graph_view_cache import flush_graph_views


def config(output):
    return RunConfig.model_validate({"sources": [], "output_dir": str(output),
                                     "model": {"mode": "in_environment"}, "scopes": ["public"]})


def graph(cfg, node="n1", label="名称"):
    return {
        "format_version": 1, "target": "graph", "status": "published",
        "support_visibility_version": 1, "semantic_origin": "agent_authored",
        "access_fingerprint": access_fingerprint(cfg),
        "definitions": {"entities": [{"type": "物品", "key_names": ["编号"]}], "relations": []},
        "nodes": [{"id": node, "type": "物品", "label": label, "identity": {"编号": node},
                   "properties": {}, "provenance": [{"source_id": "s"}],
                   "supports": [{"scope_ref": "public", "label": label,
                                 "provenance": {"source": {"source_id": "s"}, "evidence_refs": ["e1"]}}]}],
        "edges": [], "evidence_refs": ["e1"],
    }


def draft(name):
    return {"draft_id": name, "entities": [{"type": "物品", "key_names": ["编号"],
            "mappings": [{"source_id": "s", "table": "t", "key_columns": ["code"]}]}]}


def publish(store, cfg, name="draft", node="n1"):
    store.issue_evidence("schema", ref="e1")
    store.save_draft(draft(name))
    expected = graph(cfg, node)
    receipt = store.record_validation(name, revision=1, passed=True, graph=expected).validation
    assert (store.root / receipt.graph_path / "nodes.parquet").is_file()
    assert store.read_validation_graph(name, receipt) == expected
    entry = store.publish(name, revision=1, graph=expected)
    assert (store.root / entry["graph"] / "edges.parquet").is_file()
    return expected


def install_legacy_head(store, cfg, payload, relative="published/legacy.yaml"):
    write_yaml(store.root / relative, payload)
    manifest = store._manifest()
    manifest["current_graph"] = {"context_fingerprint": store.context_fingerprint,
                                 "access_fingerprint": access_fingerprint(cfg), "path": relative}
    write_yaml(store.manifest_path, manifest)


def test_preview_publication_unit_contributors_and_dag_merge_use_parquet(tmp_path):
    cfg = config(tmp_path / "project")
    contributors = []
    for unit_id in ("u1", "u2"):
        output = tmp_path / unit_id
        store = GraphWorkspace(output / "construction", unit_id)
        publish(store, cfg, node=unit_id)
        contributor = load_unit_contributor(output, unit_id)
        assert contributor["graph"] == store.read_published_graph()
        assert contributor["evidence_records"]["e1"]["kind"] == "schema"
        contributors.append(contributor)
    summary = publish_merged_contributors(cfg.output_dir, cfg, contributors=contributors,
                                         included_units=["u1", "u2"], skipped_units=[],
                                         unit_attempts={"u1": "attempt1", "u2": "attempt2"})
    assert summary["nodes"] == 2
    root = tmp_path / "project" / "construction"
    head = read_internal_yaml(root / "manifest.yaml")["current_graph"]
    assert head["storage_layout"] == "parquet_v1"
    assert head["workflow_units"] == {"u1": "attempt1", "u2": "attempt2"}
    assert (root / head["path"] / "nodes.parquet").is_file()
    merged = GraphWorkspace(root, head["context_fingerprint"]).read_published_graph()
    assert len(merged["nodes"]) == 2
    visible = load_graph(cfg)
    assert len(visible["nodes"]) == 2
    assert all(node["supports"][0]["scope_ref"] == "public" for node in visible["nodes"])
    flush_graph_views()


def test_legacy_yaml_graph_and_validation_remain_readable(tmp_path):
    cfg = config(tmp_path)
    store = GraphWorkspace(tmp_path / "construction", "ctx")
    expected = graph(cfg)
    install_legacy_head(store, cfg, expected)
    assert store.read_published_graph() == expected
    assert load_unit_contributor(tmp_path, "legacy")["graph"] == expected
    assert load_graph(cfg)["nodes"][0]["label"] == "名称"
    store.save_draft(draft("legacy"))
    receipt = store.record_validation("legacy", revision=1, passed=True, graph=expected).validation
    write_yaml(store.root / "previews/legacy.yaml", expected)
    receipt = receipt.model_copy(update={"graph_path": "previews/legacy.yaml"})
    assert store.read_validation_graph("legacy", receipt) == expected
    flush_graph_views()


def test_legacy_sharded_core_rehydrates_supports_and_provenance(tmp_path):
    cfg = config(tmp_path)
    store = GraphWorkspace(tmp_path / "construction", "ctx")
    expected = graph(cfg)
    core = deepcopy(expected)
    core["storage_layout"] = "sharded_v1"
    shard = _shard_bucket("n1")
    relative = f"published/old/evidence-shards/{shard}"
    core["evidence_shards"] = {shard: relative}
    row = core["nodes"][0]
    extras = {"supports": row.pop("supports"), "provenance": row.pop("provenance")}
    path = store.root / relative
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"nodes": {"n1": extras}, "edges": {}}), encoding="utf-8")
    install_legacy_head(store, cfg, core, "published/old/graph-core.yaml")
    loaded = store.read_published_graph()
    assert loaded["nodes"] == expected["nodes"]
    assert load_unit_contributor(tmp_path, "legacy")["graph"]["nodes"] == expected["nodes"]
    assert load_graph(cfg)["nodes"][0]["supports"] == expected["nodes"][0]["supports"]
    flush_graph_views()


def test_parquet_rows_bypass_internal_yaml_bound_and_agent_input_stays_bounded(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    store = GraphWorkspace(tmp_path / "construction", "ctx")
    monkeypatch.setattr(workspace, "INTERNAL_YAML_MAX_BYTES", 4000)
    expected = graph(cfg, label="x" * 20000)
    relative = store._append_record("previews", expected)
    assert store._read_record(relative) == expected
    write_yaml(store.root / "large-legacy.yaml", expected)
    with pytest.raises(LightGraphError, match="yaml_document_too_large"):
        store._read_record("large-legacy.yaml")
    # Agent YAML does not acquire the relaxed internal-file allowance.
    monkeypatch.setattr(workspace, "INTERNAL_YAML_MAX_BYTES", 512 * 1024 * 1024)
    with pytest.raises(LightGraphError, match="yaml_document_too_large"):
        parse_graph_draft(" " * (8 * 1024 * 1024 + 1))


def test_failed_publication_does_not_advance_head_or_prior_records(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    store = GraphWorkspace(tmp_path / "construction", "ctx")
    publish(store, cfg)
    prior_graph = store.read_published_graph()
    store.save_draft(draft("next"))
    expected = graph(cfg, "n2")
    store.record_validation("next", revision=1, passed=True, graph=expected)
    manifest_before = store.manifest_path.read_bytes()

    def fail_metadata(*args):
        raise OSError("simulated publish interruption")

    monkeypatch.setattr(graph_storage, "_publish_metadata", fail_metadata)
    with pytest.raises(OSError, match="simulated publish interruption"):
        store.publish("next", revision=1, graph=expected)
    assert store.manifest_path.read_bytes() == manifest_before
    assert store.read_published_graph() == prior_graph
    assert store.get_draft("next").status == "validated"


def test_failed_dag_merge_does_not_advance_head(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    root = tmp_path / "construction"
    write_sharded_graph(root, graph(cfg), context_fingerprint="first")
    manifest_before = (root / "manifest.yaml").read_bytes()

    def fail_rows(*args):
        raise OSError("simulated merge interruption")

    monkeypatch.setattr(graph_storage, "_write_rows", fail_rows)
    with pytest.raises(OSError, match="simulated merge interruption"):
        write_sharded_graph(root, graph(cfg, "n2"), context_fingerprint="second")
    assert (root / "manifest.yaml").read_bytes() == manifest_before
    assert GraphWorkspace(root, "first").read_published_graph()["nodes"][0]["id"] == "n1"


def test_parquet_web_view_preserves_scope_filter_and_rejects_changed_access(tmp_path):
    cfg = config(tmp_path)
    value = graph(cfg)
    private = deepcopy(value["nodes"][0])
    private.update(id="private", label="秘密", identity={"编号": "private"})
    private["supports"][0].update(scope_ref="private", label="秘密")
    value["nodes"].append(private)
    write_sharded_graph(tmp_path / "construction", value, context_fingerprint="ctx")
    visible = load_graph(cfg)
    assert [node["id"] for node in visible["nodes"]] == ["n1"]
    changed = cfg.model_copy(update={"isolation_domain": "different"})
    with pytest.raises(LightGraphError, match="graph_access_context_changed"):
        load_graph(changed)
    flush_graph_views()


@pytest.mark.parametrize('operation', ['freeze', 'copy'])
def test_inherited_snapshot_retry_preserves_old_summary_and_ignores_orphans(tmp_path, monkeypatch, operation):
    from lightgraph.construction.inheritance import freeze_inherited_graphs, frozen_contributors
    from lightgraph.construction.workflow import copy_frozen_inheritance
    cfg = config(tmp_path / 'project')
    source = tmp_path / 'base.yaml'
    write_yaml(source, graph(cfg))
    cfg = cfg.model_copy(update={'inherited_graphs': RunConfig.model_validate({
        'sources': [], 'inherited_graphs': [{'inherit_id': 'base', 'snapshot_path': str(source)}],
    }).inherited_graphs})
    parent, target = tmp_path / 'parent', tmp_path / 'target'
    freeze_inherited_graphs(cfg, parent)
    if operation == 'freeze':
        def run():
            return freeze_inherited_graphs(cfg, target)
    else:
        def run():
            return copy_frozen_inheritance(parent, target)
    run()
    summary_path = target / 'inherited/summary.yaml'
    original_summary = summary_path.read_bytes()
    original_graph = frozen_contributors(target, config=cfg)[0]
    write_rows = graph_storage._write_rows

    def interrupt_after_first_file(*args):
        write_rows(*args)
        raise OSError('simulated snapshot interruption')

    monkeypatch.setattr(graph_storage, '_write_rows', interrupt_after_first_file)
    with pytest.raises(OSError, match='simulated snapshot interruption'):
        run()
    assert summary_path.read_bytes() == original_summary
    assert frozen_contributors(target, config=cfg)[0] == original_graph
    orphans = [p for p in (target / 'inherited').glob('*.graph') if not (p / 'metadata.yaml').exists()]
    assert len(orphans) == 1
    monkeypatch.setattr(graph_storage, '_write_rows', write_rows)
    run()
    assert frozen_contributors(target, config=cfg)[0] == original_graph
    assert summary_path.read_bytes() != original_summary
    assert orphans[0].exists()
    assert all(len(p.name) <= 38 for p in (target / 'inherited').glob('*.graph'))
