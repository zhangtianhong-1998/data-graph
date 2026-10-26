"""Upgrade failures must preserve history and report committed results accurately."""
import json
import pytest
from pydantic import ValidationError

import lightgraph.pipeline as pipeline
from lightgraph.contracts import ColumnSpec, ExtractionGoal, RuleSpec, TableSpec
from lightgraph.documents import load_document
from lightgraph.errors import LightGraphError
from lightgraph.rules import RuleEngine
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore


def hierarchy_table():
    return TableSpec(source_id="s", name="paths",
        columns=[ColumnSpec(name=name) for name in
                 ("tenant", "l1_code", "l1_name", "l2_code", "l2_name")])


def hierarchy_rule(*, relative=False):
    identity={"key_columns":["tenant","l1_code"],"entity_kind":"ReportItem",
              "level_policy":"level_qualified"}
    if relative:
        identity.update(semantic_resolution="verified_identity_domain", level_policy="relative_path",
            identity_domain="report_items", identity_evidence=["approved:business_key"],
            attribute_evidence=["approved:properties"])
    return RuleSpec(rule_id="paths",revision=2 if relative else 1,kind="wide_hierarchy",
        source={"source_id":"s","table":"paths"},scope={"namespaces":["tenant"]},
        identity=identity,
        hierarchy={"taxonomy":"finance","levels":[
            {"level":number,"code":f"l{number}_code","label":f"l{number}_name"} for number in (1,2)],
            "order_evidence":["approved:parent_child"],
            "position_semantics":"ordinal_slots","position_evidence":["approved:relative_positions"]})


def path(parent, child):
    return {"tenant":"t","l1_code":parent,"l1_name":parent,"l2_code":child,"l2_name":child}


def project(store, spec, rows):
    with MemorySource([hierarchy_table()],{"public.paths":rows}) as source:
        return RuleEngine(store).execute(source,spec)


def test_invalidated_old_head_still_produces_verified_migration_audit():
    with GraphStore() as store:
        first=project(store,hierarchy_rule(),[path("A","B"),path("B","C")])
        assert first.published,first.codes
        historical=store.view({"t"},publication_id=first.publication_id)
        assert len(historical["nodes"])==4
        store.invalidate_rule("paths","discovery_goal_or_workflow_changed")
        assert store.view({"t"})["nodes"]==[]
        assert next(head for head in store.heads() if head["logical_rule_id"]=="paths")["valid"]==0

        replacement=project(store,hierarchy_rule(relative=True),[path("A","B"),path("B","C")])
        assert replacement.published,replacement.codes
        current=store.view({"t"})
        assert len(current["nodes"])==3 and len(current["edges"])==2
        audit=store.connection.execute("""SELECT m.*,p.state FROM identity_migration m
            JOIN publication p ON p.publication_id=m.new_publication""").fetchall()
        assert len(audit)==4
        assert {row["old_publication"] for row in audit}=={first.publication_id}
        assert {row["new_publication"] for row in audit}=={replacement.publication_id}
        assert {row["state"] for row in audit}=={"published"}
        old_b={node["id"] for node in historical["nodes"] if node["label"]=="B"}
        assert len(old_b)==2
        assert len({row["new_entity"] for row in audit if row["old_entity"] in old_b})==1
        assert store.view({"t"},publication_id=first.publication_id)==historical


def test_invalidated_old_head_cannot_bypass_migration_attribute_conflicts():
    with GraphStore() as store:
        first=project(store,hierarchy_rule(),[path("A","B")])
        assert first.published
        historical=store.view({"t"},publication_id=first.publication_id)
        store.invalidate_rule("paths","applicable_documents_changed")
        stale_head=next(head for head in store.heads() if head["logical_rule_id"]=="paths")

        rejected=project(store,hierarchy_rule(relative=True),[{**path("A","B"),"l2_name":"different"}])
        assert not rejected.published
        assert "identity_migration_conflict" in rejected.codes
        assert next(head for head in store.heads() if head["logical_rule_id"]=="paths")==stale_head
        assert store.view({"t"})["nodes"]==[]
        assert store.view({"t"},publication_id=first.publication_id)==historical
        assert not store.connection.execute("SELECT 1 FROM publication WHERE state IN ('staging','ready')").fetchone()

        retry=project(store,hierarchy_rule(relative=True),[path("A","B")])
        assert retry.published,retry.codes
        assert {node["label"] for node in store.view({"t"})["nodes"]}=={"A","B"}
        audit=store.connection.execute("SELECT old_publication,new_publication FROM identity_migration").fetchall()
        assert len(audit)==2
        assert {(row["old_publication"],row["new_publication"]) for row in audit}=={(first.publication_id,retry.publication_id)}


def simple_table(*, changed=False):
    names=["item_code","item_name"]+(["new_metadata_column"] if changed else [])
    return TableSpec(source_id="s",name="items",columns=[ColumnSpec(name=name) for name in names])


def run_items(config, *, changed=False, documents=()):
    row={"item_code":"001","item_name":"销售费用"}
    if changed:
        row["new_metadata_column"]="schema_revision_only"
    with MemorySource([simple_table(changed=changed)],{"public.items":[row]}) as source:
        return pipeline.run_pipeline(config,[source],documents=documents)


def test_workflow_upgrade_refreshes_metadata_with_old_document_alignment(tmp_path,monkeypatch):
    # This fixture tests publication upgrades, with its generic object type explicit.
    config=RunConfig(sources=[],output_dir=str(tmp_path),scopes=["public"],
        goal=ExtractionGoal(entity_kinds=["BusinessConcept"]))
    document=load_document("analysis.md",text=(
        "---\nid: analysis\npurpose: analysis_knowledge\n---\n"
        "# 分析方法\n销售费用建议分析地区结构。\n"))
    first=run_items(config,documents=[document])
    assert first.status in {"COMPLETED","COMPLETED_WITH_GAPS"},first.issues
    assert first.rules_published and not first.rules_skipped,first.issues
    with GraphStore(tmp_path/"graph.sqlite") as store:
        before=store.view({"public"},include_candidates=True)
        assert any(edge["relation"]=="analyzes" for edge in before["edges"])
        claim_ids={claim["fact_id"] for claim in before["claims"]}
        assert claim_ids
        heads={head["logical_rule_id"]:head for head in store.heads()}
        old_document_publication=heads["document:"+document.document_id]["publication_id"]
        old_business_publication=heads[first.rules_published[0]]["publication_id"]
        old_business_history=store.view({"public"},publication_id=old_business_publication)
        old_catalog=heads["catalog:s"]["publication_id"]

    monkeypatch.setattr(pipeline,"DISCOVERY_WORKFLOW_VERSION",
        pipeline.DISCOVERY_WORKFLOW_VERSION+"/upgrade_regression")
    upgraded=run_items(config,changed=True,documents=[document])
    assert upgraded.status in {"COMPLETED","COMPLETED_WITH_GAPS"},upgraded.issues
    assert upgraded.rules_published and not upgraded.rules_skipped,upgraded.issues
    assert not any(issue["code"]=="dangling_edge_requires_dependency_publication" for issue in upgraded.issues)
    with GraphStore(tmp_path/"graph.sqlite") as store:
        graph=store.view({"public"},include_candidates=True)
        assert any(node["kind"]=="Column" and node["label"]=="new_metadata_column" for node in graph["nodes"])
        assert any(node["layer"]=="business" and node["status"]=="accepted" and node["label"]=="销售费用"
                   for node in graph["nodes"])
        assert {claim["fact_id"] for claim in graph["claims"]}==claim_ids
        nodes={node["id"] for node in graph["nodes"]}
        assert all(edge["source"] in nodes and edge["target"] in nodes for edge in graph["edges"])
        assert any(edge["relation"]=="analyzes" for edge in graph["edges"])
        heads={head["logical_rule_id"]:head for head in store.heads()}
        assert heads["catalog:s"]["publication_id"]!=old_catalog
        assert heads["document:"+document.document_id]["publication_id"]==old_document_publication
        assert store.view({"public"},publication_id=old_business_publication)==old_business_history
        assert not store.connection.execute("SELECT 1 FROM publication WHERE state IN ('staging','ready')").fetchone()


def test_marker_failure_preserves_published_accounting_and_cleans_staging(tmp_path,monkeypatch):
    config=RunConfig(sources=[],output_dir=str(tmp_path),scopes=["public"],
        goal=ExtractionGoal(entity_kinds=["BusinessConcept"]))
    original=GraphStore.publish_partition
    failed_markers=[]

    def fail_discovery_marker(store, completed):
        if store._publication(completed.publication_id)["logical_rule_id"].startswith("discovery:"):
            failed_markers.append(completed.publication_id)
            raise LightGraphError("injected_marker_failure")
        return original(store,completed)

    monkeypatch.setattr(GraphStore,"publish_partition",fail_discovery_marker)
    report=run_items(config)
    assert report.status=="PARTIAL",report.issues
    assert report.rules_published
    assert not set(report.rules_published) & set(report.rules_skipped)
    assert any(issue["code"]=="injected_marker_failure" for issue in report.issues)
    assert len(failed_markers)==1
    saved=json.loads((tmp_path/"report.json").read_text(encoding="utf-8"))
    assert saved["rules_published"]==report.rules_published
    assert not set(saved["rules_published"]) & set(saved["rules_skipped"])
    with GraphStore(tmp_path/"graph.sqlite") as store:
        current=store.view({"public"})
        business_ids={node["id"] for node in current["nodes"] if node["layer"]=="business"}
        assert len(business_ids)==1
        published_heads={head["logical_rule_id"]:head for head in store.heads() if head["valid"]}
        assert set(report.rules_published)<=set(published_heads)
        assert store._publication(failed_markers[0])["state"]=="aborted"
        assert not store.connection.execute("SELECT 1 FROM publication WHERE state IN ('staging','ready')").fetchone()

    monkeypatch.setattr(GraphStore,"publish_partition",original)
    retry=run_items(config)
    assert retry.status in {"COMPLETED","COMPLETED_WITH_GAPS"},retry.issues
    assert not set(retry.rules_published) & set(retry.rules_skipped)
    with GraphStore(tmp_path/"graph.sqlite") as store:
        graph=store.view({"public"})
        assert {node["id"] for node in graph["nodes"] if node["layer"]=="business"}==business_ids
        marker=store.current_versions("discovery:s")
        assert marker.get("workflow")==pipeline.DISCOVERY_WORKFLOW_VERSION
        assert pipeline._context_versions(marker).keys()=={"public.items"}
        assert not store.connection.execute("SELECT 1 FROM publication WHERE state IN ('staging','ready')").fetchone()


@pytest.mark.parametrize("kept_groups", [("A",), ("A","B")])
def test_composite_key_migration_preserves_each_business_field(kept_groups):
    table=TableSpec(source_id="s",name="composite",columns=[
        ColumnSpec(name=name) for name in ("group_code","code","name")])
    old=RuleSpec(rule_id="composite",kind="column_group_entity",
        source={"source_id":"s","table":"composite"},
        identity={"key_columns":["group_code","code"],"entity_kind":"ReportItem"},
        attributes={"single_valued":{"label":"name"}})
    raw=old.model_dump()
    raw["revision"]=2
    raw["identity"].update(semantic_resolution="verified_identity_domain",identity_domain="items",
        identity_evidence=["approved:composite_business_key"],attribute_evidence=["approved:attributes"])
    new=RuleSpec.model_validate(raw)
    rows=[{"group_code":group,"code":"1","name":"same label"} for group in ("A","B")]
    with GraphStore() as store:
        with MemorySource([table],{"public.composite":rows}) as source:
            first=RuleEngine(store).execute(source,old)
        assert first.published,first.codes
        history=store.view({"public"},publication_id=first.publication_id)
        assert len(history["nodes"])==2
        old_ids={json.loads(json.loads(row["accepted_business_key"]))[0]:row["entity_id"]
                 for row in store.connection.execute("""SELECT b.accepted_business_key,r.entity_id
                     FROM source_binding b JOIN source_binding_revision r USING(binding_id)""")}
        with MemorySource([table],{"public.composite":[row for row in rows if row["group_code"] in kept_groups]}) as source:
            replacement=RuleEngine(store).execute(source,new)
        assert replacement.published,replacement.codes
        assert len(store.view({"public"})["nodes"])==len(kept_groups)
        audit=store.connection.execute("SELECT old_entity,new_entity FROM identity_migration").fetchall()
        assert len(audit)==len(kept_groups)
        assert {row["old_entity"] for row in audit}=={old_ids[group] for group in kept_groups}
        assert len({row["new_entity"] for row in audit})==len(kept_groups)
        assert store.view({"public"},publication_id=first.publication_id)==history


@pytest.mark.parametrize("kind", ["column_group_entity","long_hierarchy","reference"])
def test_relative_path_is_rejected_outside_wide_hierarchy(kind):
    raw=hierarchy_rule(relative=True).model_dump()
    raw["kind"]=kind
    if kind=="long_hierarchy":
        raw["hierarchy"]={"parent_column":"parent","order_evidence":["approved:parent"]}
    elif kind=="reference":
        raw["relation"]={"source_rule_id":"source","target_rule_id":"target",
                         "source_columns":["code"],"target_columns":["code"]}
    with pytest.raises(ValidationError,match="relative_path_requires_wide_hierarchy"):
        RuleSpec.model_validate(raw)
