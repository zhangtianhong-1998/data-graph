"""Executor acceptance focuses on outcomes across failure and dependency changes."""
from contextlib import contextmanager

import pytest

from lightgraph.contracts import ColumnSpec, TableSpec, RuleSpec, PartitionCompletion, GraphFact, SupportSpec
from lightgraph.errors import LightGraphError
from lightgraph.rules import RuleEngine
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore


def table(columns, name="items"):
    return TableSpec(source_id="s", name=name, columns=[ColumnSpec(name=c) for c in columns])


def entity_rule(rule_id="metric", kind="Metric", code="code", label="label", definition=None):
    attrs={"label": label}
    if definition: attrs["definition"]=definition
    return RuleSpec(rule_id=rule_id,kind="column_group_entity",source={"source_id":"s","table":"items"},
        scope={"namespaces":["tenant"]},
        identity={"key_columns":["tenant",code],"entity_kind":kind,
                  "semantic_resolution":"verified_definition_registry" if definition else "source_scoped",
                  "semantic_key_columns":[definition] if definition else []},
        attributes={"single_valued":attrs},evidence={"semantic_basis":["explicit_definition_registry"]})


def wide_rule(levels=3, qualified=False):
    return RuleSpec(rule_id="tree",kind="wide_hierarchy",source={"source_id":"s","table":"items"},
        scope={"namespaces":["tenant"]},identity={"key_columns":["tenant","l1_code"],"entity_kind":"ReportItem",
            "level_policy":"level_qualified" if qualified else "code_with_fill_merge"},
        hierarchy={"levels":[{"level":i,"code":f"l{i}_code","label":f"l{i}_name"} for i in range(1,levels+1)],
                   "taxonomy":"report_items","fill_semantics_confirmed":True,"order_evidence":["approved_data_description"]})


def wide_table(levels=3):
    return table(["tenant"]+[c for i in range(1,levels+1) for c in (f"l{i}_code",f"l{i}_name")])


def path(*codes):
    result={"tenant":"A"}
    for i,code in enumerate(codes,1):
        result[f"l{i}_code"]=code
        result[f"l{i}_name"]=code
    return result


def completion(pub):
    return PartitionCompletion(publication_id=pub,complete=True,consistent=True,dependencies_valid=True)


def test_two_tenants_share_confirmed_definition_and_repeat_without_extra_supports():
    t=table(["tenant","code","label","definition"])
    rows=[{"tenant":"A","code":"001","label":"销售费用","definition":"销售相关费用之和"},
          {"tenant":"B","code":"900","label":"销售费用","definition":"销售相关费用之和"}]
    with MemorySource([t],{"public.items":rows*5}) as source, GraphStore() as store:
        engine=RuleEngine(store)
        first=engine.execute(source,entity_rule(definition="definition"))
        assert first.published, first.codes
        view=store.view({"A","B"})
        assert len(view["nodes"])==1
        assert len(view["nodes"][0]["properties"]["business_code"])==2
        before={f["id"] for f in view["facts"]}
        supports={s["id"] for s in view["supports"]}
        again=engine.execute(source,entity_rule(definition="definition"))
        assert again.published,again.codes
        assert {f["id"] for f in store.view({"A","B"})["facts"]}==before
        assert {s["id"] for s in store.view({"A","B"})["supports"]}==supports


def test_changed_definition_revises_binding_and_historical_result_survives():
    t=table(["tenant","code","label","definition"])
    rule=entity_rule(definition="definition")
    with GraphStore() as store:
        engine=RuleEngine(store)
        with MemorySource([t],{"public.items":[{"tenant":"A","code":"1","label":"费用","definition":"SUM(expense)"}]}) as source:
            first=engine.execute(source,rule)
        first_id=store.view({"A"})["nodes"][0]["id"]
        with MemorySource([t],{"public.items":[{"tenant":"A","code":"1","label":"费用","definition":"AVG(expense)"}]}) as source:
            second=engine.execute(source,rule)
        assert second.published,second.codes
        assert store.view({"A"})["nodes"][0]["id"]!=first_id
        assert store.view({"A"})["supports"][0]["binding_revision"]==2
        assert store.view({"A"},publication_id=first.publication_id)["nodes"][0]["id"]==first_id


def test_distinct_column_groups_keep_local_codes_separate_and_references_correct():
    t = table(["tenant", "cost_code", "cost_name", "income_code", "income_name"])
    rules = [entity_rule("cost", "Metric", "cost_code", "cost_name"),
             entity_rule("income", "Metric", "income_code", "income_name"),
             RuleSpec(rule_id="relationship", kind="reference", source={"source_id": "s", "table": "items"},
                scope={"namespaces": ["tenant"]}, identity={"key_columns": ["tenant", "cost_code", "income_code"], "entity_kind": "Metric"},
                relation={"source_rule_id": "cost", "target_rule_id": "income", "source_columns": ["tenant", "cost_code"],
                          "target_columns": ["tenant", "income_code"], "relation": "parent_of"},
                evidence={"semantic_basis": ["confirmed_metric_relationship"]})]
    rows = [{"tenant": "A", "cost_code": "01", "cost_name": "销售费用", "income_code": "01", "income_name": "销售收入"}]
    with MemorySource([t], {"public.items": rows}) as source, GraphStore() as store:
        engine = RuleEngine(store)
        results = [engine.execute(source, rule, defer_publish=True) for rule in rules]
        assert all(r.prepared for r in results), [r.codes for r in results]
        store.publish_partitions([completion(p) for r in results for p in r.publication_ids])
        view = store.view({"A"})
        nodes = {n["label"]: n["id"] for n in view["nodes"]}
        assert set(nodes) == {"销售费用", "销售收入"}
        assert nodes["销售费用"] != nodes["销售收入"]
        assert [(e["source"], e["target"]) for e in view["edges"]] == [(nodes["销售费用"], nodes["销售收入"])]


def test_identical_lookup_columns_share_across_rule_ids():
    t = table(["tenant", "code", "label"])
    with MemorySource([t], {"public.items": tenant_rows("A")}) as source, GraphStore() as store:
        engine = RuleEngine(store)
        first = engine.execute(source, entity_rule("first"))
        second = engine.execute(source, entity_rule("second"))
        assert first.published and second.published
        assert len(store.view({"A"})["nodes"]) == 1
        assert {s["rule_id"] for s in store.view({"A"})["supports"]} == {"first", "second"}


def test_verified_definition_can_share_across_different_lookup_columns():
    t = table(["tenant", "cost_code", "cost_name", "expense_code", "expense_name", "definition"])
    rules = [entity_rule("cost", "Metric", "cost_code", "cost_name", "definition"),
             entity_rule("expense", "Metric", "expense_code", "expense_name", "definition")]
    rows = [{"tenant": "A", "cost_code": "01", "cost_name": "销售费用", "expense_code": "99", "expense_name": "销售费用", "definition": "SUM(sales_expense)"}]
    with MemorySource([t], {"public.items": rows}) as source, GraphStore() as store:
        engine = RuleEngine(store)
        assert all(engine.execute(source, rule).published for rule in rules)
        assert len(store.view({"A"})["nodes"]) == 1


def test_direct_publication_failure_keeps_original_error_and_immutable_validation():
    t = table(["tenant", "code", "label"])
    with GraphStore() as store:
        engine = RuleEngine(store)
        with MemorySource([t], {"public.items": tenant_rows("A")}) as source:
            assert engine.execute(source, entity_rule()).published
        metric = store.view({"A"})["nodes"][0]["id"]
        pub = store.begin_publication("external_reference", 1, "all", {})
        support = SupportSpec(source_binding="external_reference", scope_ref="A", partition_id="all",
            rule_id="external_reference", rule_revision=1, validation_ref="checked")
        store.stage_fact(pub, GraphFact(fact_kind="NodeExistence", payload={"entity_id": "measure", "kind": "Measure", "layer": "business"}), support)
        store.stage_fact(pub, GraphFact(fact_kind="Edge", payload={"source": metric, "relation": "uses_measure", "target": "measure"}), support)
        store.publish_partition(completion(pub))
        previous = {f["id"] for f in store.view({"A"})["facts"]}
        with MemorySource([t], {"public.items": []}) as source:
            result = engine.execute(source, entity_rule())
        assert not result.published and not result.prepared
        assert result.codes == ["dangling_edge_requires_dependency_publication"]
        assert result.validation.status == "unresolved"
        assert {f["id"] for f in store.view({"A"})["facts"]} == previous
        records = store.connection.execute("SELECT payload FROM validation_record").fetchall()
        assert len(records) == 3  # first validation, second structural validation, failed publication
        assert sum('"status":"validated"' in r[0] for r in records) == 2
        assert sum('"status":"unresolved"' in r[0] for r in records) == 1


def test_fd_failure_never_registers_validated_rule():
    t=table(["tenant","code","label"])
    rows=[{"tenant":"A","code":"1","label":"甲"},{"tenant":"A","code":"1","label":"乙"}]
    with MemorySource([t],{"public.items":rows}) as source, GraphStore() as store:
        result=RuleEngine(store).execute(source,entity_rule())
        assert not result.published and not result.prepared
        assert "fd_violation" in result.codes
        assert not store.get_rules()
        record=store.connection.execute("SELECT payload FROM validation_record").fetchone()[0]
        assert '"status":"rejected"' in record


def test_late_cycle_failure_does_not_leave_validated_rule_or_bindings():
    t=wide_table(2)
    with MemorySource([t],{"public.items":[path("A","B"),path("B","A")]}) as source, GraphStore() as store:
        engine=RuleEngine(store)
        result=engine.execute(source,wide_rule(2))
        assert "hierarchy_cycle" in result.codes
        assert not result.published and not result.prepared
        assert not store.get_rules()
        assert not engine.bindings
        record=store.connection.execute("SELECT payload FROM validation_record").fetchone()[0]
        assert '"status":"unresolved"' in record
        assert not store.view({"A"})["nodes"]


def test_snapshot_exit_failure_cannot_publish_or_expose_bindings():
    class BrokenExit(MemorySource):
        @contextmanager
        def snapshot(self):
            yield self
            raise LightGraphError("snapshot_exit_failed")
    t=table(["tenant","code","label"])
    with BrokenExit([t],{"public.items":[{"tenant":"A","code":"1","label":"甲"}]}) as source, GraphStore() as store:
        engine=RuleEngine(store)
        result=engine.execute(source,entity_rule())
        assert result.codes==["snapshot_exit_failed"]
        assert not result.published
        assert not store.heads()
        assert not store.get_rules()
        assert not engine.bindings


def test_failed_definition_update_then_restore_keeps_published_source_identity():
    class BrokenExit(MemorySource):
        @contextmanager
        def snapshot(self):
            yield self
            raise LightGraphError("snapshot_exit_failed")

    t = table(["tenant", "code", "label", "definition"])
    rule = entity_rule(definition="definition")
    rule.identity.semantic_resolution = "source_scoped"
    old_row = {"tenant": "A", "code": "1", "label": "费用", "definition": "SUM(expense)"}
    with GraphStore() as store:
        engine = RuleEngine(store)
        with MemorySource([t], {"public.items": [old_row]}) as source:
            first = engine.execute(source, rule)
        assert first.published
        original = store.view({"A"})["nodes"][0]["id"]
        with BrokenExit([t], {"public.items": [{**old_row, "definition": "AVG(expense)"}]}) as source:
            failed = engine.execute(source, rule)
        assert failed.codes == ["snapshot_exit_failed"] and not failed.published
        assert store.view({"A"})["nodes"][0]["id"] == original
        with MemorySource([t], {"public.items": [old_row]}) as source:
            restored = engine.execute(source, rule)
        assert restored.published, restored.codes
        assert store.view({"A"})["nodes"][0]["id"] == original
        assert store.view({"A"})["supports"][0]["binding_revision"] == 3


def partitioned_rule():
    rule = entity_rule()
    rule.scope.partition_by = ["tenant"]
    return rule


def tenant_rows(*tenants):
    return [{"tenant": tenant, "code": "001", "label": "费用"} for tenant in tenants]


def test_partition_enumeration_and_projection_share_one_outer_snapshot():
    class TrackingSource(MemorySource):
        entered = 0
        depth_seen = []
        @contextmanager
        def snapshot(self):
            self.entered += 1
            with super().snapshot():
                yield self
        def project(self, *args, **kwargs):
            self.depth_seen.append(self._depth)
            yield from super().project(*args, **kwargs)
    t = table(["tenant", "code", "label"])
    with TrackingSource([t], {"public.items": tenant_rows("A", "B")}) as source, GraphStore() as store:
        result = RuleEngine(store).execute(source, partitioned_rule())
        assert result.published, result.codes
        assert result.counts["partitions_enumerated"] == 2
        assert len(result.publication_ids) == len(result.active_publication_ids) == 1
        assert source.entered == 1 and source.depth_seen == [1, 1, 1]
        assert len(store.view({"A", "B"})["nodes"]) == 2
        assert {h["partition_id"] for h in store.heads()} == {"all"}
        saved = store.get_rules()[0]
        assert saved.scope.predicate is None and saved.scope.partition_by == ["tenant"]


def test_partition_deletion_zero_rows_and_retry_reconcile_only_after_complete_read():
    t = table(["tenant", "code", "label"])
    with GraphStore() as store:
        engine = RuleEngine(store)
        rule = partitioned_rule()
        for tenants, expected in ((["A", "B"], 2), (["B"], 1), ([], 0), (["B"], 1)):
            with MemorySource([t], {"public.items": tenant_rows(*tenants)}) as source:
                result = engine.execute(source, rule)
            assert result.published, result.codes
            assert len(store.view({"A", "B"})["nodes"]) == expected
        assert len(engine.bindings) == 1
        assert not store.view({"A"})["nodes"]


def test_partition_all_migration_replaces_old_heads_atomically():
    t = table(["tenant", "code", "label"])
    with GraphStore() as store:
        engine = RuleEngine(store)
        first_ids = None
        for revision, partitioned in ((1, False), (2, True), (3, False)):
            rule = partitioned_rule() if partitioned else entity_rule()
            rule.revision = revision
            with MemorySource([t], {"public.items": tenant_rows("A", "B")}) as source:
                result = engine.execute(source, rule)
            assert result.published, result.codes
            current = {n["id"] for n in store.view({"A", "B"})["nodes"]}
            first_ids = first_ids or current
            assert current == first_ids
            assert {s["rule_revision"] for s in store.view({"A", "B"})["supports"]} == {revision}
            assert len(result.publication_ids) == len(result.active_publication_ids) == 1
            assert len(store.heads()) == 1


def test_partition_enumeration_over_limit_cannot_retract_previous_partitions():
    t = table(["tenant", "code", "label"])
    with GraphStore() as store:
        engine = RuleEngine(store, max_partitions=2)
        with MemorySource([t], {"public.items": tenant_rows("A", "B")}) as source:
            assert engine.execute(source, partitioned_rule()).published
        before = store.heads()
        bindings = dict(engine.bindings)
        with MemorySource([t], {"public.items": tenant_rows("A", "B", "C")}) as source:
            failed = engine.execute(source, partitioned_rule())
        assert not failed.published and not failed.prepared
        assert store.heads() == before and engine.bindings == bindings
        assert len(store.view({"A", "B", "C"})["nodes"]) == 2


@pytest.mark.parametrize("failure_point", ["enumeration", "second_partition", "snapshot_exit"])
def test_partition_failure_aborts_whole_rule_and_retry_preserves_ids(failure_point):
    class Interrupted(MemorySource):
        calls = 0
        def project(self, *args, **kwargs):
            self.calls += 1
            yield from super().project(*args, **kwargs)
            if (failure_point == "enumeration" and self.calls == 1 or
                    failure_point == "second_partition" and self.calls == 3):
                raise LightGraphError("query_cancelled")
        @contextmanager
        def snapshot(self):
            with super().snapshot():
                yield self
            if failure_point == "snapshot_exit":
                raise LightGraphError("snapshot_exit_failed")
    t = table(["tenant", "code", "label"])
    with GraphStore() as store:
        engine = RuleEngine(store)
        with MemorySource([t], {"public.items": tenant_rows("A", "B")}) as source:
            assert engine.execute(source, partitioned_rule()).published
        before = {n["id"] for n in store.view({"A", "B"})["nodes"]}
        heads = store.heads()
        with Interrupted([t], {"public.items": tenant_rows("B", "C")}) as source:
            failed = engine.execute(source, partitioned_rule(), defer_publish=True)
        assert not failed.prepared and not failed.publication_ids
        assert store.heads() == heads
        assert {n["id"] for n in store.view({"A", "B"})["nodes"]} == before
        assert not store.connection.execute("SELECT 1 FROM publication WHERE state='staging'").fetchone()
        with MemorySource([t], {"public.items": tenant_rows("A", "B")}) as source:
            assert engine.execute(source, partitioned_rule()).published
        assert {n["id"] for n in store.view({"A", "B"})["nodes"]} == before


def test_later_partition_fd_failure_cannot_publish_earlier_partition():
    t = table(["tenant", "code", "label"])
    rows = tenant_rows("A", "B") + [{"tenant": "B", "code": "001", "label": "冲突"}]
    with MemorySource([t], {"public.items": rows}) as source, GraphStore() as store:
        result = RuleEngine(store).execute(source, partitioned_rule())
        assert result.codes == ["fd_violation"]
        assert not result.published and not store.heads() and not store.get_rules()


def test_partition_preserves_original_scope_predicate_and_per_partition_row_cap():
    t = table(["tenant", "code", "label", "active"])
    t.columns[-1].data_type = "integer"
    rows = [{**r, "active": 1} for r in tenant_rows("A", "B")]
    rows.append({"tenant": "C", "code": "excluded", "label": "不抽取", "active": 0})
    rule = partitioned_rule()
    rule.scope.predicate = {"column": "active", "op": "eq", "value": 1}
    rule.budget.max_rows_returned = 1
    with MemorySource([t], {"public.items": rows}) as source, GraphStore() as store:
        result = RuleEngine(store).execute(source, rule)
        assert result.published, result.codes
        assert len(store.view({"A", "B", "C"})["nodes"]) == 2
        assert store.get_rules()[0].scope.predicate == rule.scope.predicate


def test_partition_filter_does_not_reduce_user_predicate_depth_budget():
    t = table(["tenant", "code", "label"])
    rule = partitioned_rule()
    rule.scope.predicate = {"any": [{"not": {"column": "tenant", "op": "eq", "value": "C"}}]}
    with MemorySource([t], {"public.items": tenant_rows("A", "B", "C")}) as source, GraphStore() as store:
        result = RuleEngine(store).execute(source, rule)
        assert result.published, result.codes
        assert len(store.view({"A", "B", "C"})["nodes"]) == 2
        assert store.get_rules()[0].scope.predicate == rule.scope.predicate


def test_deferred_dependency_group_can_retract_nodes_and_edges_together():
    t=table(["tenant","metric","metric_name","measure","measure_name"])
    rules=[entity_rule("metrics","Metric","metric","metric_name"),
           entity_rule("measures","Measure","measure","measure_name"),
           RuleSpec(rule_id="dependency",kind="reference",source={"source_id":"s","table":"items"},
            scope={"namespaces":["tenant"]},identity={"key_columns":["tenant","metric","measure"],"entity_kind":"Metric"},
            relation={"source_rule_id":"metrics","target_rule_id":"measures","source_columns":["tenant","metric"],
                      "target_columns":["tenant","measure"],"relation":"uses_measure"},
            evidence={"semantic_basis":["declared_measure_dependency"]})]
    rows=[{"tenant":"A","metric":"m1","metric_name":"收入","measure":"q1","measure_name":"累计金额"}]
    with GraphStore() as store:
        engine=RuleEngine(store)
        with MemorySource([t],{"public.items":rows}) as source:
            prepared=[engine.execute(source,rule,defer_publish=True) for rule in rules]
        assert all(r.prepared and not r.published for r in prepared),[r.codes for r in prepared]
        assert not store.view({"A"})["nodes"]
        store.publish_partitions([completion(r.publication_id) for r in prepared])
        assert len(store.view({"A"})["nodes"])==2
        assert len(store.view({"A"})["edges"])==1
        with MemorySource([t],{"public.items":[]}) as source:
            prepared=[engine.execute(source,rule,defer_publish=True) for rule in rules]
        assert all(r.prepared for r in prepared)
        assert not engine.bindings
        store.publish_partitions([completion(r.publication_id) for r in prepared])
        assert not store.view({"A"})["nodes"]
        assert not store.view({"A"})["edges"]


def test_partitioned_reference_keeps_all_endpoint_bindings_and_retracts_tenant():
    t = table(["tenant", "metric", "metric_name", "measure", "measure_name"])
    rules = [entity_rule("metrics", "Metric", "metric", "metric_name"),
             entity_rule("measures", "Measure", "measure", "measure_name"),
             RuleSpec(rule_id="dependency", kind="reference", source={"source_id": "s", "table": "items"},
                scope={"namespaces": ["tenant"]}, identity={"key_columns": ["tenant", "metric", "measure"], "entity_kind": "Metric"},
                relation={"source_rule_id": "metrics", "target_rule_id": "measures", "source_columns": ["tenant", "metric"],
                          "target_columns": ["tenant", "measure"], "relation": "uses_measure"},
                evidence={"semantic_basis": ["declared_measure_dependency"]})]
    for rule in rules:
        rule.scope.partition_by = ["tenant"]
    rows = [{"tenant": tenant, "metric": "m1", "metric_name": "收入", "measure": "q1", "measure_name": "累计金额"}
            for tenant in ("A", "B")]
    with GraphStore() as store:
        engine = RuleEngine(store)
        for current, nodes, edges in ((rows, 4, 2), (rows[1:], 2, 1)):
            with MemorySource([t], {"public.items": current}) as source:
                results = [engine.execute(source, rule, defer_publish=True) for rule in rules]
            assert all(r.prepared for r in results), [r.codes for r in results]
            store.publish_partitions([completion(p) for r in results for p in r.publication_ids])
            view = store.view({"A", "B"})
            assert len(view["nodes"]) == nodes and len(view["edges"]) == edges
        assert not store.view({"A"})["nodes"]


@pytest.mark.parametrize("codes",[("A",None,"A"),("A","B","A")])
def test_non_contiguous_same_code_requires_level_qualified_identity(codes):
    t=wide_table()
    with MemorySource([t],{"public.items":[path(*codes)]}) as source, GraphStore() as store:
        rejected=RuleEngine(store).execute(source,wide_rule())
        assert "non_contiguous_level_code" in rejected.codes
        assert not rejected.published
    with MemorySource([t],{"public.items":[path(*codes)]}) as source, GraphStore() as store:
        accepted=RuleEngine(store).execute(source,wide_rule(qualified=True))
        assert accepted.published,accepted.codes


def test_continuous_confirmed_fill_merges_and_gap_does_not_connect():
    t=wide_table()
    with MemorySource([t],{"public.items":[path("A","A","A")]}) as source, GraphStore() as store:
        result=RuleEngine(store).execute(source,wide_rule())
        assert result.published,result.codes
        assert len(store.view({"A"})["nodes"])==1
        assert not store.view({"A"})["edges"]
    with MemorySource([t],{"public.items":[path("A",None,"C")]}) as source, GraphStore() as store:
        result=RuleEngine(store).execute(source,wide_rule())
        assert result.published,result.codes
        assert "gap_in_path" in result.codes
        assert len(store.view({"A"})["nodes"])==2
        assert not store.view({"A"})["edges"]


def test_cross_row_different_level_code_without_fill_evidence_is_rejected():
    t=wide_table(2)
    with MemorySource([t],{"public.items":[path("A","B"),path("B","C")]}) as source, GraphStore() as store:
        result=RuleEngine(store).execute(source,wide_rule(2))
        assert "cross_level_code_reuse" in result.codes
        assert not result.published


def test_failed_attempt_restores_previously_published_bindings():
    t=table(["tenant","code","label"])
    with GraphStore() as store:
        engine=RuleEngine(store)
        with MemorySource([t],{"public.items":[{"tenant":"A","code":"old","label":"Old"}]}) as source:
            assert engine.execute(source,entity_rule()).published
        prior=dict(engine.bindings)
        with MemorySource([t],{"public.items":[{"tenant":"A","code":"new","label":"甲"},
                                                 {"tenant":"A","code":"new","label":"乙"}]}) as source:
            assert not engine.execute(source,entity_rule()).published
        assert engine.bindings==prior


def array_setup(store):
    t=table(["tenant","metric","metric_name","measure","measure_name","allowed_measures"])
    registry_rules=[entity_rule("metrics","Metric","metric","metric_name"),
                    entity_rule("measures","Measure","measure","measure_name")]
    reference=RuleSpec(rule_id="array_dependency",kind="reference",source={"source_id":"s","table":"items"},
        scope={"namespaces":["tenant"]},identity={"key_columns":["tenant","metric"],"entity_kind":"Metric"},
        relation={"source_rule_id":"metrics","target_rule_id":"measures","source_columns":["tenant","metric"],
                  "target_columns":["tenant","allowed_measures"],"target_json_array_columns":["allowed_measures"],
                  "relation":"uses_measure"},evidence={"semantic_basis":["approved_json_reference"]})
    rows=[{"tenant":"A","metric":"m1","metric_name":"收入","measure":code,"measure_name":name,"allowed_measures":'["01","02","01"]'}
          for code,name in [("01","累计"),("02","同比")]]
    engine=RuleEngine(store)
    with MemorySource([t],{"public.items":rows}) as source:
        for rule in registry_rules:
            outcome=engine.execute(source,rule)
            assert outcome.published,outcome.codes
        outcome=engine.execute(source,reference)
        assert outcome.published,outcome.codes
    return t,engine,reference,rows


def test_json_reference_expansion_preserves_zeroes_deduplicates_and_empty_retracts():
    with GraphStore() as store:
        t,engine,reference,rows=array_setup(store)
        assert len(store.view({"A"})["edges"])==2
        for row in rows:row["allowed_measures"]="[]"
        with MemorySource([t],{"public.items":rows}) as source:
            result=engine.execute(source,reference)
        assert result.published,result.codes
        assert not store.view({"A"})["edges"]
        assert len(store.view({"A"})["nodes"])==3


@pytest.mark.parametrize("value,code",[
    ('not JSON','invalid_json_reference'),
    ('{}','invalid_json_reference_array'),
    ('[null]','non_scalar_json_reference'),
    ('[["01"]]','non_scalar_json_reference'),
    ('[{"code":"01"}]','non_scalar_json_reference'),
    ('[NaN]','non_scalar_json_reference'),
    ('["01","missing"]','reference_missing'),
    ('[1]','reference_missing'),
    (None,'reference_partial_null'),
])
def test_invalid_json_reference_never_partially_replaces_existing_edges(value,code):
    with GraphStore() as store:
        t,engine,reference,rows=array_setup(store)
        previous={e["id"] for e in store.view({"A"})["edges"]}
        for row in rows:row["allowed_measures"]=value
        with MemorySource([t],{"public.items":rows}) as source:
            result=engine.execute(source,reference)
        assert not result.published and not result.prepared
        assert code in result.codes
        assert {e["id"] for e in store.view({"A"})["edges"]}==previous


@pytest.mark.parametrize("limit_type,code",[("array","json_reference_element_limit"),("total","reference_expansion_limit")])
def test_json_expansion_budget_preserves_previous_publication(limit_type,code):
    with GraphStore() as store:
        t,engine,reference,rows=array_setup(store)
        reference=reference.model_copy(deep=True)
        reference.revision=2
        if limit_type=="array":reference.relation.max_array_elements=1
        else:reference.budget.max_rows_returned=1
        with MemorySource([t],{"public.items":rows}) as source:
            result=engine.execute(source,reference)
        assert not result.published and code in result.codes
        assert len(store.view({"A"})["edges"])==2


def test_source_side_json_array_expands_without_two_sided_product():
    row={"tenant":"A","metric_array":'["001","002","001"]',"measure":"M"}
    assert RuleEngine._reference_keys(row,["tenant","metric_array"],["metric_array"],10)==[("A","001"),("A","002")]
    from pydantic import ValidationError
    from lightgraph.contracts import RelationSpec
    with pytest.raises(ValidationError):
        RelationSpec(source_rule_id="a",target_rule_id="b",source_columns=["x"],target_columns=["y"],
                     source_json_array_columns=["x"],target_json_array_columns=["y"])


def test_pipeline_two_runs_and_dependency_delete_are_consistent(tmp_path):
    from lightgraph.pipeline import run_pipeline
    from lightgraph.runtime.config import RunConfig
    t=table(["tenant","metric","metric_name","measure","measure_name"])
    rules=[entity_rule("metrics","Metric","metric","metric_name"),
           entity_rule("measures","Measure","measure","measure_name"),
           RuleSpec(rule_id="dependency",kind="reference",source={"source_id":"s","table":"items"},
            scope={"namespaces":["tenant"]},identity={"key_columns":["tenant","metric","measure"],"entity_kind":"Metric"},
            relation={"source_rule_id":"metrics","target_rule_id":"measures","source_columns":["tenant","metric"],
                      "target_columns":["tenant","measure"],"relation":"uses_measure"},
            evidence={"semantic_basis":["declared_measure_dependency"]})]
    config=RunConfig(sources=[{"id":"s","kind":"excel_workbook","path":"supplied_in_memory"}],
                     output_dir=str(tmp_path),scopes=["A","public"])
    rows=[{"tenant":"A","metric":"m1","metric_name":"收入","measure":"q1","measure_name":"累计金额"}]
    with MemorySource([t],{"public.items":rows}) as source:
        first=run_pipeline(config,[source],rules=rules)
    assert first.status=="COMPLETED",first.issues
    with GraphStore(tmp_path/"graph.sqlite") as store:
        before={f["id"] for f in store.view({"A"})["facts"]}
        supports={s["id"] for s in store.view({"A"})["supports"]}
    class MustNotCallModel:
        usage={"calls":0}
        def complete(self,*args,**kwargs):
            raise AssertionError("unchanged_cached_rules_must_not_call_model")
    with MemorySource([t],{"public.items":rows}) as source:
        second=run_pipeline(config,[source],rules=None,model=MustNotCallModel())
    assert second.status=="COMPLETED",second.issues
    assert second.counts["llm_calls"]==0
    with GraphStore(tmp_path/"graph.sqlite") as store:
        assert {f["id"] for f in store.view({"A"})["facts"]}==before
        assert {s["id"] for s in store.view({"A"})["supports"]}==supports
    with MemorySource([t],{"public.items":[]}) as source:
        third=run_pipeline(config,[source],rules=None,model=MustNotCallModel())
    assert third.status=="COMPLETED",third.issues
    with GraphStore(tmp_path/"graph.sqlite") as store:
        assert not store.view({"A"})["nodes"]
        assert not store.view({"A"})["edges"]
