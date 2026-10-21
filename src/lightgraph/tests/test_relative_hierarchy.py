"""Outcome checks for identity, path positions, root projections and publication."""
import copy
import json
import subprocess
import sys

import pytest
from pydantic import ValidationError

from lightgraph.contracts import ColumnSpec, TableSpec, RuleSpec, PartitionCompletion, ExtractionGoal
from lightgraph.discovery.engine import _compile_model, _column_id
from lightgraph.discovery.models import ProposalBatch
from lightgraph.errors import LightGraphError
from lightgraph.rules import RuleEngine, HierarchyView, rooted_view
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore


def rule(rule_id="tree", table_name="items", *, domain="report_items", taxonomy="finance",
         positions=(1, 2), condition=None, scope="namespace_local"):
    return RuleSpec(rule_id=rule_id, kind="wide_hierarchy",
        source={"source_id": "s", "table": table_name}, scope={"namespaces": ["tenant"]},
        identity={"key_columns": ["tenant", f"l{positions[0]}_code"], "entity_kind": "ReportItem",
            "semantic_resolution": "verified_identity_domain", "level_policy": "relative_path",
            "identity_domain": domain, "identity_evidence": ["approved:business_key_domain"],
            "attribute_evidence": ["approved:attribute_support"], "identity_scope": scope,
            "global_scope_evidence": ["approved:global_business_key"] if scope=="domain_global" else []},
        hierarchy={"levels": [{"level": p, "code": f"l{p}_code", "label": f"l{p}_name"} for p in positions],
            "taxonomy": taxonomy, "order_evidence": ["approved:parent_child_order"],
            "position_semantics": "ordinal_slots", "position_evidence": ["approved:relative_ordinal_slots"],
            "context_qualifiers": {"condition": condition} if condition else {}})


def table(name="items", positions=(1, 2)):
    return TableSpec(source_id="s", name=name, columns=[ColumnSpec(name=c) for c in
        ["tenant"]+[col for p in positions for col in (f"l{p}_code", f"l{p}_name")]])


def path(*codes, positions=None, tenant="t"):
    result={"tenant":tenant}
    for p,code in zip(positions or range(1, len(codes)+1), codes):
        result[f"l{p}_code"]=code
        result[f"l{p}_name"]=code
    return result


def execute(store, spec, rows):
    positions=[level.level for level in spec.hierarchy.levels]
    with MemorySource([table(spec.source.table, positions)],
                      {f"public.{spec.source.table}": rows}) as source:
        return RuleEngine(store).execute(source, spec)


def labels(graph):
    return {node["label"]:node["id"] for node in graph["nodes"]}


def test_relative_rows_share_but_keep_each_column_binding_and_position():
    with GraphStore() as store:
        result=execute(store, rule(), [path("A","B"),path("B","C")])
        assert result.published,result.codes
        graph=store.view({"t"})
        assert len(graph["nodes"])==3 and len(graph["edges"])==2
        b=labels(graph)["B"]
        membership=[fact for fact in graph["facts"] if fact["fact_kind"]=="PropertyValue"
            and fact["payload"]["entity_id"]==b and fact["payload"]["name"]=="source_position"]
        assert {fact["payload"]["value"] for fact in membership}=={1,2}
        bindings={support["source_binding"] for support in graph["supports"]
            if support["fact_id"] in {fact["id"] for fact in membership}}
        assert len(bindings)==2
        assert all("source_level" not in edge["qualifiers"] for edge in graph["edges"])


def test_cross_table_shared_relation_support_retraction_and_different_domain():
    with GraphStore() as store:
        assert execute(store, rule("one","one"),[path("A","B")]).published
        assert execute(store, rule("two","two"),[path("A","B"),path("B","C")]).published
        graph=store.view({"t"})
        assert len(graph["nodes"])==3 and len(graph["edges"])==2
        ab=next(edge for edge in graph["edges"] if edge["source"]==labels(graph)["A"])
        assert len(ab["support_ids"])==2
        assert execute(store,rule("one","one"),[]).published
        graph=store.view({"t"})
        assert len(graph["nodes"])==3 and len(graph["edges"])==2
        assert execute(store,rule("other","other",domain="unrelated"),[path("A","B")]).published
        assert len(store.view({"t"})["nodes"])==5


def test_taxonomy_does_not_split_entity_and_root_view_does_not_change_facts():
    with GraphStore() as store:
        assert execute(store,rule(),[path("A","B"),path("B","C")]).published
        assert execute(store,rule("other","other",taxonomy="management"),[path("B","A")]).published
        graph=store.view({"t"}); ids=labels(graph)
        assert len(ids)==3 and len(graph["edges"])==3
        before=copy.deepcopy(graph)
        a=rooted_view(store,{"t"},HierarchyView(taxonomy="finance",root_entity=ids["A"]))
        b=rooted_view(store,{"t"},HierarchyView(taxonomy="finance",root_entity=ids["B"]))
        assert {n["label"]:n["display_depth"] for n in a["nodes"]}=={"A":0,"B":1,"C":2}
        assert {n["label"]:n["display_depth"] for n in b["nodes"]}=={"B":0,"C":1}
        assert store.view({"t"})==before
        with pytest.raises(LightGraphError, match="view_root_not_visible"):
            rooted_view(store,set(),HierarchyView(taxonomy="finance",root_entity=ids["B"]))


def test_cli_hierarchy_view_non_root_context_and_scope_preserve_source_facts(tmp_path):
    database=tmp_path/"graph.sqlite"
    spec=rule()
    spec.hierarchy.context_qualifiers={"hierarchy_version":"v1","period":2026}
    spec.hierarchy.semantic_levels={1:"parent_role",2:"child_role"}
    other=rule("other","other")
    other.hierarchy.context_qualifiers={"hierarchy_version":"v2","period":2026}
    with GraphStore(database) as store:
        assert execute(store,spec,[path("A","B"),path("B","C")]).published
        assert execute(store,other,[path("B","D")]).published
        before=store.view({"t"})
        ids=labels(before)
        heads=store.heads()
        assert any(edge["source"]==ids["A"] and edge["target"]==ids["B"]
                   for edge in before["edges"])

    command=[sys.executable,"-m","lightgraph.cli","hierarchy-view",str(database),
        "--root",ids["B"],"--taxonomy","finance",
        "--context","hierarchy_version=v1","--context","period=2026"]
    result=subprocess.run(command+["--scope","t"],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,(result.stdout,result.stderr)
    projection=json.loads(result.stdout)
    assert {node["label"]:node["display_depth"] for node in projection["nodes"]}=={"B":0,"C":1}
    assert [(edge["source"],edge["target"]) for edge in projection["edges"]]==[(ids["B"],ids["C"])]
    assert projection["definition"]["context_qualifiers"]=={"hierarchy_version":"v1","period":2026}
    memberships=[item for item in projection["memberships"] if item["entity_id"]==ids["B"]]
    assert {(item["source_position"],item["source_group"],item["semantic_level"])
        for item in memberships}=={
            (1,"s.public.items:l1_code","parent_role"),
            (2,"s.public.items:l2_code","child_role")}
    for item in projection["memberships"]:
        assert item["hierarchy_version"]=="v1" and item["period"]==2026
        fact=next(fact for fact in before["facts"] if fact["id"]==item["fact_id"])
        assert item["support_ids"]==fact["support_ids"] and item["support_ids"]

    denied=subprocess.run(command+["--scope","other"],capture_output=True,text=True,timeout=30)
    assert denied.returncode==1,denied.stderr
    assert json.loads(denied.stdout)=={"status":"FAILED","code":"view_root_not_visible"}
    with GraphStore(database) as store:
        assert store.view({"t"})==before
        assert store.heads()==heads


@pytest.mark.parametrize("positions,codes", [((1,2,3),("A",None,"C")),((1,3),("A","C"))])
def test_null_and_absent_slots_never_bridge(positions,codes):
    with GraphStore() as store:
        result=execute(store,rule(positions=positions),[path(*codes,positions=positions)])
        assert result.published,result.codes
        assert len(store.view({"t"})["nodes"])==2
        assert store.view({"t"})["edges"]==[]
        assert "gap_in_path" in result.codes


def test_numbering_meaning_must_be_explicit_but_order_only_slots_can_connect():
    spec=rule(positions=(1,3))
    spec.hierarchy.position_semantics=None
    with GraphStore() as store:
        result=execute(store,spec,[path("A","C",positions=(1,3))])
        assert not result.published and "hierarchy_position_semantics_missing" in result.codes
    spec.hierarchy.position_semantics="ordered_slots"
    with GraphStore() as store:
        result=execute(store,spec,[path("A","C",positions=(1,3))])
        assert result.published and len(store.view({"t"})["edges"])==1


def test_cross_rule_cycle_rejected_but_replacing_old_edges_is_allowed():
    with GraphStore() as store:
        assert execute(store,rule("one","one"),[path("A","B")]).published
        old=store.view({"t"})
        cycle=execute(store,rule("two","two"),[path("B","A")])
        assert not cycle.published and "hierarchy_cycle" in cycle.codes
        assert store.view({"t"})==old
        replacement=execute(store,rule("one","one"),[path("B","A")])
        assert replacement.published,replacement.codes
        assert len(store.view({"t"})["edges"])==1


def test_multiple_parent_is_contextual_and_dag_still_forbids_cycles():
    with GraphStore() as store:
        assert execute(store,rule("one","one"),[path("A","C")]).published
        result=execute(store,rule("two","two"),[path("B","C")])
        assert not result.published and "multiple_parent" in result.codes
        result=execute(store,rule("condition","condition",condition="budget"),[path("B","C")])
        assert result.published,result.codes
    with GraphStore() as store:
        first=rule("one","one");first.hierarchy.allow_dag=True
        second=rule("two","two");second.hierarchy.allow_dag=True
        assert execute(store,first,[path("A","C")]).published
        assert execute(store,second,[path("B","C")]).published
        third=rule("three","three");third.hierarchy.allow_dag=True
        cycle=execute(store,third,[path("C","A")])
        assert not cycle.published and "hierarchy_cycle" in cycle.codes
    with GraphStore() as store:
        assert execute(store,rule("one","one"),[path("A","B")]).published
        second=rule("two","two");second.hierarchy.allow_dag=True
        assert execute(store,second,[path("A","B")]).published
        graph=store.view({"t"})
        assert len(graph["edges"])==1 and len(graph["edges"][0]["support_ids"])==2


def test_same_code_label_conflict_is_unresolved_and_namespace_sharing_needs_authorization():
    with GraphStore() as store:
        assert execute(store,rule(),[path("A","B")]).published
        other=path("A","B");other["l2_name"]="different"
        result=execute(store,rule("other","other"),[other])
        assert not result.published and "identity_domain_attribute_conflict" in result.codes
        assert len(store.view({"t"})["nodes"])==2
    for scope,expected in [("namespace_local",4),("domain_global",2)]:
        with GraphStore() as store:
            result=execute(store,rule(scope=scope),[path("A","B",tenant="one"),path("A","B",tenant="two")])
            assert result.published,result.codes
            assert len(store.view({"one","two"})["nodes"])==expected


def test_relative_contract_cannot_be_enabled_without_identity_evidence():
    raw=rule().model_dump()
    raw["identity"]["identity_evidence"]=[]
    with pytest.raises(ValidationError,match="verified_identity_domain_evidence_required"):
        RuleSpec.model_validate(raw)
    raw=rule().model_dump();raw["identity"]["semantic_resolution"]="source_scoped"
    with pytest.raises(ValidationError,match="relative_path_requires_verified_identity_domain"):
        RuleSpec.model_validate(raw)


def proposal_fixture():
    t=table()
    ref="spec@1:hierarchy"
    doc={"doc_id":"spec","revision":"1","section_id":"hierarchy","heading":"scope","locator":"spec#hierarchy",
         "evidence_role":"definition_evidence","read_state":"complete",
         "text":"身份域 items 的业务键是编码，同一实体可在不同相对路径位置出现；属性支持授权共享。层级体系 finance 按父到子层级顺序。ordinal_slots 编号缺失表示缺层。"}
    claim={"group_id":"items","levels":[{"position":p,"key":_column_id(t,f"l{p}_code"),"label":_column_id(t,f"l{p}_name")} for p in (1,2)],
        "namespace_columns":[_column_id(t,"tenant")],"path_semantics":"relative_path","identity_domain":"items",
        "identity_evidence":[ref],"attribute_evidence":[ref],"taxonomy":"finance","taxonomy_evidence":[ref],
        "position_semantics":"ordinal_slots","position_evidence":[ref],"ordering_evidence":[ref]}
    batch=ProposalBatch.model_validate({"task":"T3_hierarchy","proposals":[
        {"kind":"wide_hierarchy","evidence_cited":[ref],"claim":claim}]})
    return t,doc,batch


def test_agent_relative_proposal_requires_registered_complete_definition_evidence():
    t,doc,batch=proposal_fixture()
    compiled=_compile_model(batch,t,ExtractionGoal(),[doc],[])
    assert compiled[0].identity.level_policy=="relative_path"
    with GraphStore() as store:
        with MemorySource([t],{"public.items":[path("A","B"),path("B","C")]}) as source:
            result=RuleEngine(store).execute(source,compiled[0],document_versions={"spec@1"})
        assert result.published,result.codes
        assert len(store.view({"t"})["nodes"])==3
    for change in [{"read_state":"partial"},{"evidence_role":"exploration_guidance"},{"text":"同名同码，似乎相同"},
                   {"sources":[{"source_id":"s","schema":"public","table":"unrelated"}]}]:
        with pytest.raises(LightGraphError):
            _compile_model(batch,t,ExtractionGoal(),[{**doc,**change}],[])
    batch.proposals[0].claim.identity_evidence=["invented@1:claim"]
    with pytest.raises(LightGraphError,match="identity_domain_evidence_unresolved"):
        _compile_model(batch,t,ExtractionGoal(),[doc],[])


def legacy_rule(rule_id="tree", table_name="items"):
    raw=rule(rule_id,table_name).model_dump()
    raw["identity"]={"key_columns":["tenant","l1_code"],"entity_kind":"ReportItem",
                     "level_policy":"level_qualified"}
    return RuleSpec.model_validate(raw)


def test_migration_candidates_history_properties_and_remaining_source_support():
    from lightgraph.identity.migration import plan_identity_migration
    with GraphStore() as store:
        old=execute(store,legacy_rule(),[path("A","B"),path("B","C")])
        assert old.published,old.codes
        history=store.view({"t"},publication_id=old.publication_id)
        assert len(history["nodes"])==4
        replacement=rule();replacement.revision=2
        with MemorySource([table()],{"public.items":[path("A","B"),path("B","C")]}) as source:
            new=RuleEngine(store).execute(source,replacement,defer_publish=True)
        assert new.prepared,new.codes
        plan=plan_identity_migration(store,new.publication_ids)
        assert plan and all(item.status=="verified" for item in plan)
        b_ids={node["id"] for node in history["nodes"] if node["label"]=="B"}
        b_plan=[item for item in plan if item.old_entity in b_ids]
        assert len({item.old_entity for item in b_plan})==2
        assert len({item.new_entity for item in b_plan})==1
        store.publish_partitions([PartitionCompletion(publication_id=pub,complete=True,
            consistent=True,dependencies_valid=True) for pub in new.publication_ids])
        current=store.view({"t"})
        assert len(current["nodes"])==3 and len(current["edges"])==2
        assert all(support["rule_revision"]==2 for support in current["supports"])
        migration_rows=store.connection.execute("""SELECT m.*,p.state FROM identity_migration m
            JOIN publication p ON p.publication_id=m.new_publication""").fetchall()
        assert len(migration_rows)==4
        assert all(row["state"]=="published" and row["identity_domain"]=="report_items" for row in migration_rows)
        assert store.view({"t"},publication_id=old.publication_id)==history
        assert execute(store,rule("other","other"),[path("A","B"),path("B","C")]).published
        assert execute(store,replacement,[]).published
        current=store.view({"t"})
        assert len(current["nodes"])==3 and len(current["edges"])==2
        assert {support["rule_id"] for support in current["supports"]}=={"other"}


def test_migration_attribute_conflict_keeps_old_publication():
    from lightgraph.identity.migration import plan_identity_migration
    with GraphStore() as store:
        assert execute(store,legacy_rule(),[path("A","B")]).published
        before=store.view({"t"})
        replacement=rule();replacement.revision=2
        altered=path("A","B");altered["l2_name"]="different"
        with MemorySource([table()],{"public.items":[altered]}) as source:
            new=RuleEngine(store).execute(source,replacement,defer_publish=True)
        assert new.prepared,new.codes
        plan=plan_identity_migration(store,new.publication_ids)
        assert any("migration_attribute_conflict" in item.conflicts for item in plan)
        with pytest.raises(LightGraphError,match="identity_migration_conflict"):
            store.publish_partitions([PartitionCompletion(publication_id=pub,complete=True,
                consistent=True,dependencies_valid=True) for pub in new.publication_ids])
        assert store.view({"t"})==before
        for pub in new.publication_ids:
            store.abort(pub)
        retried=execute(store,replacement,[path("A","B")])
        assert retried.published,retried.codes
        assert set(labels(store.view({"t"})))=={"A","B"}


def test_two_prepared_rules_cannot_publish_conflicting_shared_attributes():
    with GraphStore() as store:
        results=[]
        for spec,rows in [(rule("one","one"),[path("A","B")]),
                          (rule("two","two"),[{**path("A","B"),"l2_name":"different"}])]:
            with MemorySource([table(spec.source.table)],{f"public.{spec.source.table}":rows}) as source:
                result=RuleEngine(store).execute(source,spec,defer_publish=True)
                assert result.prepared,result.codes
                results.extend(result.publication_ids)
        with pytest.raises(LightGraphError,match="identity_domain_attribute_conflict"):
            store.publish_partitions([PartitionCompletion(publication_id=pub,complete=True,
                consistent=True,dependencies_valid=True) for pub in results])
        assert store.view({"t"})["nodes"]==[]


def test_hierarchy_versions_are_distinct_contexts_for_validation_and_root_views():
    with GraphStore() as store:
        one=rule("one","one")
        one.hierarchy.context_qualifiers={"hierarchy_version":"v1","valid_period":"2025"}
        two=rule("two","two")
        two.hierarchy.context_qualifiers={"hierarchy_version":"v2","valid_period":"2026"}
        assert execute(store,one,[path("A","B")]).published
        assert execute(store,two,[path("B","A")]).published
        graph=store.view({"t"})
        assert len(graph["nodes"])==2 and len(graph["edges"])==2
        view=store.hierarchy_view({"t"},HierarchyView(taxonomy="finance",
            root_entity=labels(graph)["B"],context_qualifiers={"hierarchy_version":"v2","valid_period":"2026"}))
        assert {node["label"]:node["display_depth"] for node in view["nodes"]}=={"B":0,"A":1}
        assert all(member["hierarchy_version"]=="v2" for member in view["memberships"])
