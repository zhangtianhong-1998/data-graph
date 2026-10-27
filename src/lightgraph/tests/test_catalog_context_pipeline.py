"""Exercise metadata outputs and document changes through the real pipeline."""
import json

import pytest
import yaml

from lightgraph.catalog import declared_catalog, semantic_catalog, write_catalog
from lightgraph.contracts import ColumnSpec, TableSpec, ExtractionGoal, ForeignKeySpec
from lightgraph.documents import load_document
from lightgraph.metadata import publish_metadata
from lightgraph.pipeline import run_pipeline
from lightgraph.runtime.config import RunConfig
from lightgraph.runtime.retention import retain_check
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import client, input_packet, response, group, supported_semantic_review


def table(name, source="s", columns=("key", "label", "amount")):
    return TableSpec(source_id=source, name=name,
                     columns=[ColumnSpec(name=c,comment=c) for c in columns])


def test_catalog_keeps_declaration_goal_revisions_and_never_observations(tmp_path, monkeypatch):
    t=table("items")
    sentinel="DO_NOT_STORE_OBSERVATION_84ad21"
    def handler(request):
        _,p=input_packet(request)
        return response(group(p,"key","label",extra=[
            ("amount",{"role":"fact_value","exclude_from_projection":True})]))
    config=RunConfig(sources=[],output_dir=str(tmp_path/"out"))
    model=client(monkeypatch,handler)
    with MemorySource([t],{"public.items":[("001","销售费用",sentinel)]}) as source:
        report=run_pipeline(config,[source],model=model)
        assert report.rules_published,report.issues
        first=yaml.safe_load((tmp_path/"out/metadata.yaml").read_text(encoding="utf-8"))
        calls=model.usage["calls"]
        repeated=run_pipeline(config,[source],model=model)
        assert repeated.counts["reused_rules"]==1
        assert model.usage["calls"]==calls
        # Roles intentionally excluded from graph projection still belong in
        # the data description; a cache hit must not erase that interpretation.
        assert yaml.safe_load((tmp_path/"out/metadata.yaml").read_text(encoding="utf-8"))==first
        config.goal.purpose="保留用于经营分析的定义，排除观测金额"
        report=run_pipeline(config,[source],model=model)
        assert report.rules_published,report.issues
        second=yaml.safe_load((tmp_path/"out/metadata.yaml").read_text(encoding="utf-8"))
    assert first["declared"]==second["declared"]
    assert first["semantic_annotations"]["goal_revision"]!=second["semantic_annotations"]["goal_revision"]
    assert second["declared"]["tables"][0]["columns"][0]["comment"]=="key"
    annotations={c["column"]:c for c in second["semantic_annotations"]["tables"][0]["columns"]}
    assert annotations["label"]["role"]=="label"
    assert annotations["label"]["status"]=="rule_validated"
    assert annotations["amount"]["role"]=="fact_value"
    assert len(list((tmp_path/"out/metadata/declarations").glob("*.yaml")))==1
    assert retain_check([tmp_path/"out"],[sentinel])["violations"]==0


def test_new_table_document_rediscovers_only_affected_table(tmp_path, monkeypatch):
    tables=[table("a"),table("b")]
    seen=[]
    def handler(request):
        _,p=input_packet(request)
        seen.append((p["task"],p["scope"]["table"]))
        if p["task"]=="T2_cross_table":
            return response({"task":p["task"],"abstain":True,"proposals":[]})
        return response(group(p,"key","label"))
    config=RunConfig(sources=[],output_dir=str(tmp_path/"out"))
    doc=load_document("a.md",text=(
        "---\nid: a_description\npurpose: data_description\n"
        "applies_to: ['s:public.a']\n---\n# 说明\n编码和名称对应同一个分析对象。"))
    model=client(monkeypatch,handler)
    with MemorySource(tables,{"public.a":[("1","A","10")],"public.b":[("2","B","20")]}) as source:
        first=run_pipeline(config,[source],model=model)
        assert len(first.rules_published)==2,first.issues
        seen.clear()
        second=run_pipeline(config,[source],model=model,documents=[doc])
        assert second.rules_published,second.issues
        assert [(stage,name) for stage,name in seen if stage=="T0_discovery"]==[("T0_discovery","a")]
        seen.clear()
        third=run_pipeline(config,[source],model=model,documents=[doc])
        assert third.counts["reused_rules"]==2,third.issues
        assert seen==[('T2_cross_table','a'),('T2_cross_table','b'),
                      ('T1_table_discovery','a'),('T1_table_discovery','b')]
        assert any(i['code']=='unexplained_columns' for i in third.issues)


def test_composite_constraints_keep_order_and_group(tmp_path):
    parent=table("parent",columns=("tenant","code","label"))
    parent.unique_keys=[["tenant","code"]]
    child=table("child",columns=("namespace","ref"))
    child.foreign_keys=[ForeignKeySpec(name="two_part_fk",columns=["namespace","ref"],
        target_schema="public",target_table="parent",target_columns=["tenant","code"])]
    with MemorySource([parent,child],{"public.parent":[],"public.child":[]}) as source, GraphStore() as store:
        publish_metadata(store,source)
        graph=store.view({"public"})
        constraint=next(n for n in graph["nodes"] if n["kind"]=="Constraint" and n["label"]=="two_part_fk")
        local=[e for e in graph["edges"] if e["source"]==constraint["id"] and e["relation"]=="constraint_column"]
        remote=[e for e in graph["edges"] if e["source"]==constraint["id"] and e["relation"]=="constraint_target"]
        assert {e["qualifiers"]["column_position"] for e in local}=={1,2}
        assert {e["qualifiers"]["column_position"] for e in remote}=={1,2}
        assert len([n for n in graph["nodes"] if n["kind"]=="Constraint"])==2


def test_catalog_rejects_tampered_revision_before_write(tmp_path):
    with MemorySource([table("a")],{"public.a":[]}) as source:
        declaration=declared_catalog([source])
        interpretation=semantic_catalog(declaration,ExtractionGoal())
    bad=declaration.model_copy(update={"revision":"../../outside"})
    with pytest.raises(ValueError):
        write_catalog(tmp_path,bad,interpretation)
    assert not list(tmp_path.iterdir())


def test_cross_source_agent_reference_is_verified_and_published(tmp_path, monkeypatch):
    metric=table("metrics","database",("metric","title","measure_ref"))
    measure=table("measures","file",("code","title"))
    doc=load_document("relationship.md",text=(
        "---\nid: relation\npurpose: data_description\n---\n"
        "# 含义\n指标使用度量。指标表的 measure_ref 引用度量表的 code。"))
    def handler(request):
        _,p=input_packet(request)
        if p['task']=='semantic_review':return response(supported_semantic_review(p))
        if p["task"]=="T0_discovery":
            if p["scope"]["source_id"]=="database":
                return response(group(p,"metric","title",kind="Metric"))
            return response(group(p,"code","title",kind="Measure"))
        if p["scope"]["source_id"]!="database":
            return response({"task":p["task"],"abstain":True,"proposals":[]})
        ids={c["name"]:c["id"] for c in p["metadata_digest"]["columns"]}
        bindings={b["entity_kind"]:b["rule_id"] for b in p["entity_bindings"]}
        return response({"task":p["task"],"proposals":[{"kind":"reference","claim":{
            "source_entity":bindings["Metric"],"target_entity":bindings["Measure"],
            "source_columns":[ids["metric"]],"target_columns":[ids["measure_ref"]],
            "relation_kind":"uses_measure"},"evidence_cited":[ids["measure_ref"]]}]})
    config=RunConfig(sources=[],output_dir=str(tmp_path/"out"))
    with MemorySource([metric],{"public.metrics":[("M1","收入","SUM")]}) as a, \
         MemorySource([measure],{"public.measures":[("SUM","累计金额")]}) as b:
        report=run_pipeline(config,[a,b],model=client(monkeypatch,handler),documents=[doc])
        assert len(report.rules_published)==3,report.issues
    with GraphStore(tmp_path/"out/graph.sqlite") as store:
        graph=store.view({"public"})
        assert len([edge for edge in graph["edges"] if edge["relation"]=="uses_measure"])==1
        assert len([node for node in graph["nodes"] if node["layer"]=="business"])==2
