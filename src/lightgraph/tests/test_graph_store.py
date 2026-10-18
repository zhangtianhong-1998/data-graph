"""Behavioral tests for shared identities, authorization and atomic partition replacement."""
from __future__ import annotations

import sqlite3
import pytest

from lightgraph.contracts import GraphFact, SupportSpec, PartitionCompletion, RuleSpec
from lightgraph.identity import IdentityConflict
from lightgraph.store import GraphStore, StoreError, PublicationConflict, IncompletePartition, PersistenceViolation


def node(entity="entity_shared", domain="default"):
    return GraphFact(isolation_domain=domain, fact_kind="NodeExistence", payload={"entity_id": entity, "kind": "Metric", "layer": "business"})


def prop(name, value, entity="entity_shared", domain="default"):
    return GraphFact(isolation_domain=domain, fact_kind="PropertyValue", payload={"entity_id": entity, "name": name, "value": value})


def edge(source, target, relation="parent_of"):
    return GraphFact(fact_kind="Edge", payload={"source": source, "target": target, "relation": relation, "qualifiers": {}})


def support(scope="A", rule="registry", revision=1, partition=None, status="accepted", validation="v1"):
    return SupportSpec(source_binding="binding_" + scope, scope_ref=scope, partition_id=partition or scope,
                       rule_id=rule, rule_revision=revision, validation_ref=validation, status=status)


def complete(pub, **kwargs):
    return PartitionCompletion(publication_id=pub, complete=True, consistent=True, dependencies_valid=True, **kwargs)


def publish(store, facts, scope="A", rule="registry", revision=1, status="accepted", validation="v1"):
    pub = store.begin_publication(rule, revision, scope, {"schema": "schema1"})
    for fact in facts:
        store.stage_fact(pub, fact, support(scope, rule, revision, status=status, validation=validation))
    store.publish_partition(complete(pub))
    return pub


def test_shared_identity_properties_acl_search_and_withdrawal():
    with GraphStore() as store:
        a = publish(store, [node(), prop("label", "销售费用"), prop("alias", "A私有别名")])
        publish(store, [node(), prop("label", "销售费用"), prop("alias", "B别名")], "B")
        assert len(store.view({"A", "B"})["nodes"]) == 1
        assert store.view({"B"})["nodes"][0]["aliases"] == ["B别名"]
        assert not store.search("A私有别名", {"B"})
        assert "A私有别名" not in str(store.export({"B"}))
        publish(store, [], "A")
        current = store.view({"A", "B"})
        assert len(current["nodes"]) == 1
        assert current["nodes"][0]["aliases"] == ["B别名"]
        assert not store.view({"A"})["nodes"]
        assert store.view({"A"}, publication_id=a)["nodes"][0]["aliases"] == ["A私有别名"]


def test_same_fact_different_rules_and_revisions_keep_one_fact():
    with GraphStore() as store:
        publish(store, [node(), prop("label", "收入")], rule="r1")
        publish(store, [node(), prop("label", "收入")], rule="r2")
        view = store.view({"A"})
        assert len(view["facts"]) == 2
        assert len(view["supports"]) == 4
        publish(store, [node(), prop("label", "收入")], rule="r1", revision=2)
        assert len(store.view({"A"})["facts"]) == 2
        assert len(store.view({"A"})["supports"]) == 4
        publish(store, [], rule="r1", revision=2)
        assert len(store.view({"A"})["facts"]) == 2
        assert {s["rule_id"] for s in store.view({"A"})["supports"]} == {"r2"}


@pytest.mark.parametrize("field,value", [("complete", False), ("consistent", False), ("dependencies_valid", False), ("truncated", True)])
def test_incomplete_partition_cannot_retract(field, value):
    with GraphStore() as store:
        publish(store, [node()])
        pub = store.begin_publication("registry", 1, "A", {})
        completion = complete(pub).model_copy(update={field: value})
        with pytest.raises(IncompletePartition):
            store.publish_partition(completion)
        assert len(store.view({"A"})["nodes"]) == 1
        assert store._publication(pub)["state"] == "staging"


def test_generation_cas_prevents_stale_worker_replacement():
    with GraphStore() as store:
        publish(store, [node()])
        older = store.begin_publication("registry", 1, "A", {})
        newer = store.begin_publication("registry", 1, "A", {})
        store.stage_fact(newer, node(), support())
        store.publish_partition(complete(newer))
        with pytest.raises(PublicationConflict):
            store.publish_partition(complete(older))
        assert store.heads()[0]["publication_id"] == newer
        assert store.heads()[0]["generation"] == 2


def test_crash_in_commit_keeps_old_head_and_watermark(tmp_path, monkeypatch):
    path = tmp_path / "graph.sqlite"
    store = GraphStore(path)
    original = publish(store, [node()])
    pub = store.begin_publication("registry", 1, "A", {})
    monkeypatch.setattr(store, "_advance_watermarks", lambda: (_ for _ in ()).throw(RuntimeError("simulated_process_failure")))
    with pytest.raises(RuntimeError):
        store.publish_partition(complete(pub, checkpoint_ref="cursor2"))
    store.close()
    with GraphStore(path) as recovered:
        assert recovered.heads()[0]["publication_id"] == original
        assert recovered.heads()[0]["checkpoint_ref"] is None
        assert recovered._publication(pub)["state"] == "staging"
        assert recovered.recover_staging() == [pub]
        assert len(recovered.view({"A"})["nodes"]) == 1


def test_publication_evidence_is_immutable_and_historical():
    with GraphStore() as store:
        old = publish(store, [node()], validation="v1")
        new = publish(store, [node()], validation="v2")
        assert store.view({"A"}, publication_id=old)["supports"][0]["validation_ref"] == "v1"
        assert store.view({"A"}, publication_id=new)["supports"][0]["validation_ref"] == "v2"
        pub = store.begin_publication("registry", 1, "A", {})
        store.stage_fact(pub, node(), support(validation="v3"))
        with pytest.raises(StoreError, match="immutable"):
            store.stage_fact(pub, node(), support(validation="v4"))


def test_candidate_scope_cannot_borrow_accepted_status_or_label():
    with GraphStore() as store:
        publish(store, [node(), prop("label", "A机密指标")], "A")
        publish(store, [node()], "B", status="candidate")
        assert not store.view({"B"})["nodes"]
        b = store.view({"B"}, include_candidates=True)["nodes"][0]
        assert b["status"] == "candidate"
        assert b["label"] == "entity_shared"
        assert "A机密指标" not in str(b)


def test_staging_cannot_be_read_exported_or_searched():
    with GraphStore() as store:
        pub = store.begin_publication("registry", 1, "A", {})
        store.stage_fact(pub, node(), support())
        store.stage_fact(pub, prop("label", "未发布"), support())
        assert not store.search("未发布", {"A"})
        assert not store.export({"A"})["nodes"]
        with pytest.raises(StoreError):
            store.view({"A"}, publication_id=pub)
        assert store.canary_check(pub)["published"] is False


def test_dangling_edge_blocks_retraction_until_dependency_group_replaced():
    with GraphStore() as store:
        publish(store, [node("parent"), node("child")])
        publish(store, [edge("parent", "child")], rule="hierarchy")
        retract_node = store.begin_publication("registry", 1, "A", {})
        with pytest.raises(StoreError, match="dangling_edge"):
            store.publish_partition(complete(retract_node))
        retract_edge = store.begin_publication("hierarchy", 1, "A", {})
        store.publish_partitions([complete(retract_node), complete(retract_edge)])
        assert not store.view({"A"})["nodes"]
        assert not store.view({"A"})["edges"]


def test_untyped_raw_rows_and_unknown_properties_rejected():
    with GraphStore() as store:
        pub = store.begin_publication("registry", 1, "A", {})
        with pytest.raises(PersistenceViolation):
            store.stage_fact(pub, {"rows": [["sensitive-canary"]]}, support())
        with pytest.raises(PersistenceViolation):
            store.stage_fact(pub, prop("raw_row", "sensitive-canary"), support())
        with pytest.raises(PersistenceViolation):
            store.stage_fact(pub, prop("description", {"raw_row": "sensitive-canary"}), support())
        assert store.connection.execute("SELECT COUNT(*) FROM graph_fact").fetchone()[0] == 0


def test_watermark_waits_for_all_partitions():
    with GraphStore() as store:
        store.register_window("w1", "gauss1", "log_position_20", [("registry", "A"), ("registry", "B")], enumeration_complete=True)
        pubs = {}
        for scope in ("A", "B"):
            pubs[scope] = store.begin_publication("registry", 1, scope, {})
            store.stage_fact(pubs[scope], node(), support(scope))
            store.attach_window_publication("w1", pubs[scope])
        store.publish_partition(complete(pubs["A"]))
        assert store.watermark("gauss1") is None
        store.publish_partition(complete(pubs["B"]))
        assert store.watermark("gauss1")["checkpoint_ref"] == "log_position_20"


def test_incomplete_partition_enumeration_never_advances_watermark():
    with GraphStore() as store:
        store.register_window("w1", "gauss1", "log_position_20", [("registry", "A")], enumeration_complete=False)
        pub = publish(store, [node()])
        store.attach_window_publication("w1", pub)
        assert store.watermark("gauss1") is None


def test_later_window_cannot_skip_incomplete_earlier_source_window():
    with GraphStore() as store:
        store.register_window("w1", "gauss1", "log_position_20", [("registry", "A")], enumeration_complete=False)
        store.register_window("w2", "gauss1", "log_position_30", [("registry", "B")], enumeration_complete=True)
        pub = publish(store, [node()], "B")
        store.attach_window_publication("w2", pub)
        assert store.watermark("gauss1") is None


def test_finished_older_window_is_valid_after_one_head_advances():
    with GraphStore() as store:
        store.register_window("w1", "gauss1", "log_position_20", [("registry", "A"), ("registry", "B")], enumeration_complete=True)
        store.register_window("w2", "gauss1", "log_position_30", [("registry", "A")], enumeration_complete=True)
        a1 = publish(store, [node()], "A")
        store.attach_window_publication("w1", a1)
        a2 = publish(store, [node()], "A")
        store.attach_window_publication("w2", a2)
        assert store.watermark("gauss1") is None
        b1 = publish(store, [node()], "B")
        store.attach_window_publication("w1", b1)
        assert store.watermark("gauss1")["window_id"] == "w2"


def rule_spec(rule_id="registry", revision=1):
    return RuleSpec(rule_id=rule_id, revision=revision, kind="column_group_entity",
                    source={"source_id": "s", "schema": "public", "table": "items"},
                    identity={"key_columns": ["code"], "entity_kind": "Metric"},
                    dependencies={"schema": "schema1", "documents": ["doc1@1"]}, status="validated")


def test_rule_revisions_immutable_and_rollback_needs_current_dependencies():
    with GraphStore() as store:
        rule = rule_spec()
        store.put_rule(rule)
        mutation = rule.model_copy(deep=True)
        mutation.identity.key_columns = ["other_code"]
        with pytest.raises(StoreError, match="immutable"):
            store.put_rule(mutation)
        current = rule.dependencies.model_dump(mode="json", by_alias=True)
        assert store.prepare_rule_rollback(rule.rule_id, 1, current).revision == 1
        assert not store.heads()
        with pytest.raises(StoreError, match="incompatible"):
            store.prepare_rule_rollback(rule.rule_id, 1, {**current, "schema": "schema2"})


def test_rule_operator_cannot_smuggle_source_rows_to_result_store():
    with GraphStore() as store:
        rule = rule_spec()
        from lightgraph.contracts import OperatorSpec
        rule.operators = [OperatorSpec(op="distinct_projection", args={"rows": [["secret-canary"]]})]
        with pytest.raises(PersistenceViolation):
            store.put_rule(rule)
        assert not store.get_rules()


def test_dependency_invalidation_hides_current_results_and_keeps_history():
    with GraphStore() as store:
        store.put_rule(rule_spec())
        history = publish(store, [node()])
        assert store.invalidate_rule("registry") == ["registry"]
        assert not store.get_rules()
        assert not store.view({"A"})["nodes"]
        assert store.view({"A"}, publication_id=history)["nodes"]
        pub = store.begin_publication("registry", 1, "A", {})
        with pytest.raises(StoreError, match="stale"):
            store.publish_partition(complete(pub))


def test_database_support_requires_validation_and_relation_status_registry():
    with GraphStore() as store:
        pub = store.begin_publication("registry", 1, "A", {})
        with pytest.raises(PersistenceViolation):
            store.stage_fact(pub, node(), support(validation=None))
        with pytest.raises(PersistenceViolation):
            store.stage_fact(pub, edge("a", "b", "technical_link"), support())


def test_explicit_scope_and_domain_protection():
    with GraphStore() as store:
        publish(store, [node("customer_a", domain="customer_a")])
        assert not store.view({"A"})["nodes"]
        assert not store.view(set(), domain="customer_a")["nodes"]
        assert store.view({"A"}, domain="customer_a")["nodes"][0]["id"] == "customer_a"
        with pytest.raises(StoreError):
            store.view("*")


def test_stable_identity_sharing_requires_definition_and_verified_mapping():
    with GraphStore() as store:
        identity = store.identity
        definition = {"label": "销售费用", "unit": "元", "formula": "SUM(expense)", "period": "YTD"}
        a = identity.resolve(kind="Metric", source_ref="s1", namespace="A", business_key="001", definition=definition, shared_verified=True)
        b = identity.resolve(kind="Metric", source_ref="s2", namespace="B", business_key="998", definition=definition, shared_verified=True)
        assert a.entity_id == b.entity_id
        c = identity.resolve(kind="Metric", source_ref="s3", namespace="C", business_key="001", definition={**definition, "formula": "AVG(expense)"}, shared_verified=True)
        assert c.entity_id != a.entity_id
        weak = identity.resolve(kind="Metric", source_ref="s4", namespace="D", business_key="001", definition={"label": "销售费用"}, shared_verified=True)
        assert weak.entity_id != a.entity_id
        other_domain = identity.resolve(kind="Metric", source_ref="s1", namespace="A", business_key="001", definition=definition, shared_verified=True, isolation_domain="customer2")
        assert other_domain.entity_id != a.entity_id


def test_label_changes_keep_identity_but_definition_changes_revision():
    with GraphStore() as store:
        args = dict(kind="Metric", source_ref="s1", namespace="A", business_key="001", shared_verified=True)
        first = store.identity.resolve(**args, definition={"label": "费用", "unit": "元", "formula": "SUM(x)"})
        rename = store.identity.resolve(**args, definition={"label": "销售费用", "unit": "元", "formula": "SUM(x)"})
        assert rename.entity_id == first.entity_id
        changed = store.identity.resolve(**args, definition={"label": "销售费用", "unit": "元", "formula": "AVG(x)"})
        assert changed.entity_id != first.entity_id
        assert changed.revision == 2
        assert store.identity.lookup(first.binding_id, revision=1).entity_id == first.entity_id
        other = store.identity.resolve(kind="Metric", source_ref="s2", namespace="B", business_key="77",
            shared_verified=True, definition={"label": "费用合计", "unit": "元", "formula": "SUM(x)"})
        assert other.entity_id == first.entity_id


def test_identity_preserves_leading_zero_type_case_and_whitespace():
    with GraphStore() as store:
        args = dict(kind="ReportItem", source_ref="s", namespace="n")
        keys = ["001", "1", 1, True, " 1", "a", "A", None, ""]
        identities = [store.identity.resolve(**args, business_key=k).entity_id for k in keys]
        assert len(set(identities)) == len(keys)


@pytest.mark.parametrize("definition", [{"unit": "元"}, {"taxonomy": "组织"}, {"period": "YTD"}])
def test_shared_context_without_identifying_definition_cannot_merge(definition):
    with GraphStore() as store:
        args = dict(kind="Metric", source_ref="s", namespace="n", definition=definition, shared_verified=True)
        a = store.identity.resolve(**args, business_key="a")
        b = store.identity.resolve(**args, business_key="b")
        assert a.entity_id != b.entity_id


def test_identity_policy_upgrade_merges_and_withdrawal_restores_source_identities():
    with GraphStore() as store:
        args = dict(kind="Metric", source_ref="s", business_key="001",
                    definition={"definition": "SUM(expense)"}, semantic_namespace="finance")
        original = {scope: store.identity.resolve(**args, namespace=scope) for scope in ("A", "B")}
        assert original["A"].entity_id != original["B"].entity_id
        shared = {scope: store.identity.resolve(**args, namespace=scope, shared_verified=True)
                  for scope in ("A", "B")}
        assert shared["A"].entity_id == shared["B"].entity_id
        assert all(r.revision == 2 for r in shared.values())
        for scope in ("A", "B"):
            local = store.identity.resolve(**args, namespace=scope)
            assert local.entity_id == original[scope].entity_id
            assert local.revision == 3
            assert store.identity.lookup(local.binding_id, 2).entity_id == shared[scope].entity_id


def test_first_shared_identity_splits_when_sharing_is_cancelled():
    with GraphStore() as store:
        args = dict(kind="Metric", source_ref="s", business_key="001", definition={"formula": "SUM(x)"})
        shared = [store.identity.resolve(**args, namespace=scope, shared_verified=True) for scope in ("A", "B")]
        assert shared[0].entity_id == shared[1].entity_id
        local = [store.identity.resolve(**args, namespace=scope) for scope in ("A", "B")]
        assert len({r.entity_id for r in local} | {shared[0].entity_id}) == 3
        assert all(r.revision == 2 for r in local)


def test_semantic_registry_change_is_a_binding_revision_and_return_reuses_identity():
    with GraphStore() as store:
        args = dict(kind="Metric", source_ref="s", namespace="A", business_key="001",
                    definition={"definition": "SUM(x)"}, shared_verified=True)
        first = store.identity.resolve(**args, semantic_namespace="sales")
        moved = store.identity.resolve(**args, semantic_namespace="finance")
        assert first.entity_id != moved.entity_id and moved.revision == 2
        restored = store.identity.resolve(**args, semantic_namespace="sales")
        assert restored.entity_id == first.entity_id and restored.revision == 3


def test_source_identity_history_survives_failed_definition_attempt():
    with GraphStore() as store:
        args = dict(kind="Metric", source_ref="s", namespace="A", business_key="001")
        first = store.identity.resolve(**args, definition={"formula": "SUM(x)"})
        failed_attempt = store.identity.resolve(**args, definition={"formula": "AVG(x)"})
        restored = store.identity.resolve(**args, definition={"formula": "SUM(x)"})
        assert failed_attempt.entity_id != first.entity_id
        assert restored.entity_id == first.entity_id and restored.revision == 3
        again = store.identity.resolve(**args, definition={"formula": "SUM(x)"})
        assert again == restored


def test_legacy_identity_store_migration_preserves_existing_binding(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with GraphStore(path) as store:
        args = dict(kind="Metric", source_ref="s", namespace="A", business_key="001",
                    definition={"formula": "SUM(x)"}, shared_verified=True, semantic_namespace="finance")
        first = store.identity.resolve(**args)
        store.connection.execute("ALTER TABLE source_binding_revision DROP COLUMN resolution_policy")
        store.connection.execute("ALTER TABLE source_binding_revision DROP COLUMN semantic_namespace")
        store.connection.execute("ALTER TABLE source_binding_revision DROP COLUMN semantic_signature")
    with GraphStore(path) as store:
        restored = store.identity.resolve(**args)
        assert restored == first


def test_publish_is_idempotent_but_history_cannot_be_reactivated():
    with GraphStore() as store:
        old = publish(store, [node()])
        assert store.publish_partition(complete(old))["generation"] == 1
        publish(store, [node()])
        with pytest.raises(PublicationConflict):
            store.publish_partition(complete(old))


def test_label_conflict_never_picks_private_global_label():
    with GraphStore() as store:
        publish(store, [node(), prop("label", "甲")], "A")
        publish(store, [node(), prop("label", "乙")], "B")
        assert store.view({"A"})["nodes"][0]["label"] == "甲"
        merged = store.view({"A", "B"})["nodes"][0]
        assert merged["label"] == "entity_shared"
        assert merged["conflicts"][0]["values"] == ["乙", "甲"]


def test_node_label_unsupported_property_retraction_without_node_delete():
    with GraphStore() as store:
        publish(store, [node(), prop("label", "可撤回标签")], "A")
        publish(store, [node()], "B")
        publish(store, [], "A")
        assert store.view({"B"})["nodes"][0]["label"] == "entity_shared"
        assert not store.search("可撤回标签", {"A", "B"})


def test_document_claims_preserve_modality_conditions_and_retract_per_document():
    with GraphStore() as store:
        claim = GraphFact(fact_kind="DocumentClaim", payload={"id": "claim1", "subject": "利润下降", "predicate": "recommends_check",
            "object": "检查销售费用", "modality": "recommended", "conditions": {"0": "排除汇率影响后"}})
        publications = []
        for document in ("doc1", "doc2"):
            pub = store.begin_publication("doc_rule", 1, document, {"document": document + "@1"})
            supp = SupportSpec(origin="document_claim", source_binding=document, scope_ref="A", partition_id=document,
                               rule_id="doc_rule", rule_revision=1, document_ref=document + "@1#section1", status="candidate")
            store.stage_fact(pub, claim, supp)
            store.publish_partition(complete(pub))
            publications.append(pub)
        assert not store.view({"A"})["claims"]
        candidate = store.view({"A"}, include_candidates=True)["claims"]
        assert len(candidate) == 1
        assert candidate[0]["modality"] == "recommended"
        pub = store.begin_publication("doc_rule", 2, "doc1", {"document": "doc1@2"})
        store.publish_partition(complete(pub))
        candidate = store.view({"A"}, include_candidates=True)["claims"]
        assert len(candidate) == 1 and len(candidate[0]["support_ids"]) == 1


def test_claim_identity_ignores_document_origin_and_condition_order():
    meaning = {"subject": "利润下降", "predicate": "recommends_check", "object": "检查费用", "modality": "recommended"}
    a = GraphFact(fact_kind="DocumentClaim", payload={**meaning, "id": "doc1-section1", "conditions": {"0": "同比", "1": "同口径"}})
    b = GraphFact(fact_kind="DocumentClaim", payload={**meaning, "id": "doc2-section8", "conditions": {"0": "同口径", "1": "同比"}})
    assert a.fact_id == b.fact_id
    assert a.payload["id"] == b.payload["id"]


def test_registered_relation_rejects_wrong_endpoint_kinds():
    with GraphStore() as store:
        publish(store, [node("a"), node("b")])
        pub = store.begin_publication("ref", 1, "A", {})
        store.stage_fact(pub, edge("a", "b", "verified_reference"), support(rule="ref"))
        with pytest.raises(StoreError, match="endpoint_kind"):
            store.publish_partition(complete(pub))
        assert not store.view({"A"})["edges"]


def test_registered_binding_revision_must_match_fact_entity():
    with GraphStore() as store:
        args = dict(kind="Metric", source_ref="source", namespace="A", business_key="001")
        first = store.identity.resolve(**args, definition={"formula": "SUM(x)"})
        second = store.identity.resolve(**args, definition={"formula": "AVG(x)"})
        pub = store.begin_publication("registry", 1, "A", {})
        stale = support().model_copy(update={"source_binding": first.binding_id, "binding_revision": first.revision})
        with pytest.raises(StoreError, match="revision_entity"):
            store.stage_fact(pub, node(second.entity_id), stale)
        current = stale.model_copy(update={"binding_revision": second.revision})
        store.stage_fact(pub, node(second.entity_id), current)
        store.publish_partition(complete(pub))
        assert store.view({"A"})["supports"][0]["binding_revision"] == 2


def test_identity_change_requires_all_using_rules_to_move_atomically():
    with GraphStore() as store:
        args = dict(kind="Metric", source_ref="source", namespace="A", business_key="001")
        first = store.identity.resolve(**args, definition={"formula": "SUM(x)"})
        for rule in ("r1", "r2"):
            pub = store.begin_publication(rule, 1, "A", {})
            supp = support(rule=rule).model_copy(update={"source_binding": first.binding_id, "binding_revision": first.revision})
            store.stage_fact(pub, node(first.entity_id), supp)
            store.publish_partition(complete(pub))
        second = store.identity.resolve(**args, definition={"formula": "AVG(x)"})
        replacements = []
        for rule in ("r1", "r2"):
            pub = store.begin_publication(rule, 2, "A", {})
            supp = support(rule=rule, revision=2).model_copy(update={"source_binding": second.binding_id, "binding_revision": second.revision})
            store.stage_fact(pub, node(second.entity_id), supp)
            replacements.append(pub)
        with pytest.raises(StoreError, match="binding_revision_requires_dependency"):
            store.publish_partition(complete(replacements[0]))
        store.publish_partitions([complete(pub) for pub in replacements])
        assert [n["id"] for n in store.view({"A"})["nodes"]] == [second.entity_id]
