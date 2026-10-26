"""Complete rule generations keep query batch identities out of durable state."""
import json
import subprocess
import sys
from contextlib import contextmanager

import pytest

from lightgraph.contracts import GraphFact, SupportSpec, digest
from lightgraph.errors import LightGraphError
from lightgraph.rules import RuleEngine
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore, PublicationConflict, StoreError
from lightgraph.tests.test_rules_engine import (
    table, partitioned_rule, tenant_rows, completion, entity_rule,
)
from lightgraph.tests.test_graph_store import node, edge, publish


def assert_no_partition_tokens(store, values):
    durable = "\n".join(store.connection.iterdump())
    for value in values:
        assert "partition_" + digest(["tenant"], [value]) not in durable
    assert "batch_0" not in durable
    assert "batch_1" not in durable


@pytest.mark.parametrize("failure", [None, "fd", "cancel", "snapshot_exit"])
def test_success_and_failure_records_never_persist_source_partition_hash(failure):
    class Source(MemorySource):
        calls = 0

        def project(self, *args, **kwargs):
            self.calls += 1
            yield from super().project(*args, **kwargs)
            if failure == "cancel" and self.calls == 3:
                raise LightGraphError("query_cancelled")

        @contextmanager
        def snapshot(self):
            with super().snapshot():
                yield self
            if failure == "snapshot_exit":
                raise LightGraphError("snapshot_exit_failed")

    values = ["private_A", "private_B"]
    rows = tenant_rows(*values)
    if failure == "fd":
        rows.append({"tenant": "private_B", "code": "001", "label": "conflict"})
    with Source([table(["tenant", "code", "label"])], {"public.items": rows}) as source, GraphStore() as store:
        result = RuleEngine(store).execute(source, partitioned_rule())
        assert result.published is (failure is None), result.codes
        assert_no_partition_tokens(store, values)
        validations = [json.loads(row[0]) for row in store.connection.execute("SELECT payload FROM validation_record")]
        assert validations and all(v["coverage"]["partitions"] == ["all"] for v in validations)
        assert all(v["coverage"]["consistent"] is (failure is None) for v in validations)
        if failure:
            assert not store.view(set(values))["nodes"]
            assert not store.connection.execute("SELECT 1 FROM publication WHERE state='staging'").fetchone()


def test_reordering_and_restarting_queries_preserves_fact_and_support_ids(tmp_path):
    path = tmp_path / "graph.sqlite"
    source_table = table(["tenant", "code", "label"])
    with GraphStore(path) as store:
        with MemorySource([source_table], {"public.items": tenant_rows("A", "B")}) as source:
            assert RuleEngine(store).execute(source, partitioned_rule()).published
        before = store.view({"A", "B"})
        fact_ids = {f["id"] for f in before["facts"]}
        support_ids = {s["id"] for s in before["supports"]}
        # Simulate a process stopping after complete staging but before publish.
        with MemorySource([source_table], {"public.items": tenant_rows("B", "C")}) as source:
            unfinished = RuleEngine(store).execute(source, partitioned_rule(), defer_publish=True)
        assert unfinished.prepared
    with GraphStore(path) as store:
        assert store.recover_staging() == unfinished.publication_ids
        assert {f["id"] for f in store.view({"A", "B"})["facts"]} == fact_ids
        with MemorySource([source_table], {"public.items": tenant_rows("B", "A")}) as source:
            assert RuleEngine(store).execute(source, partitioned_rule()).published
        after = store.view({"A", "B"})
        assert {f["id"] for f in after["facts"]} == fact_ids
        assert {s["id"] for s in after["supports"]} == support_ids
        assert len(store.heads()) == 1
        assert_no_partition_tokens(store, ["A", "B", "C"])


def test_legacy_partition_heads_replace_atomically_without_copying_tokens():
    from lightgraph.runtime.partition_audit import audit_legacy_partition_tokens
    with GraphStore() as store:
        source_table = table(["tenant", "code", "label"])
        with MemorySource([source_table], {"public.items": tenant_rows("A", "B")}) as source:
            original = RuleEngine(store).execute(source, partitioned_rule())
        original_rows = store._fact_rows(original.publication_ids)
        identities = {n["id"] for n in store.view({"A", "B"})["nodes"]}
        legacy_ids = []
        for scope in ("A", "B"):
            token = "partition_" + digest(["tenant"], [scope])
            publication = store.begin_publication("metric", 1, token, {})
            legacy_ids.append(publication)
            for row in original_rows:
                if row["scope_ref"] != scope:
                    continue
                store.stage_fact(publication,
                    GraphFact(isolation_domain=row["isolation_domain"], fact_kind=row["fact_kind"], payload=json.loads(row["payload"])),
                    SupportSpec(source_binding=row["binding_id"], scope_ref=scope, partition_id=token,
                        rule_id="metric", rule_revision=1, validation_ref=row["validation_ref"], binding_revision=row["binding_revision"]))
            store.publish_partition(completion(publication))
        # Fixture represents the old release, which had no full-rule head.
        store.connection.execute("DELETE FROM partition_head WHERE logical_rule_id='metric' AND partition_id='all'")
        with MemorySource([source_table], {"public.items": tenant_rows("B")}) as source:
            replacement = RuleEngine(store).execute(source, partitioned_rule(), defer_publish=True)
        assert replacement.prepared
        assert {n["id"] for n in store.view({"A", "B"})["nodes"]} == identities
        store.publish_partitions([completion(p) for p in replacement.publication_ids])
        assert len(store.heads()) == 1 and store.heads()[0]["partition_id"] == "all"
        assert not store.view({"A"})["nodes"]
        assert len(store.view({"B"})["nodes"]) == 1
        assert all(s["partition_id"] == "all" for s in store.view({"A", "B"})["supports"])
        # Prior publications remain available for an explicit history audit.
        assert store.view({"A"}, publication_id=legacy_ids[0])["nodes"]
        base = store.connection.execute("SELECT base_heads FROM rule_generation WHERE publication_id=?", (replacement.publication_id,)).fetchone()[0]
        assert "partition_" not in base and "A" not in base and "B" not in base
        assert len(store.connection.execute("SELECT 1 FROM publication WHERE partition_id LIKE 'partition_%'").fetchall()) == 2
        audit = audit_legacy_partition_tokens(store.connection)
        assert audit["counts"]["partition_head"] == 0
        assert audit["counts"]["publication"] == 2
        assert audit["migration_required"] and not audit["modified"]
        assert "partition_" + digest(["tenant"], ["A"]) not in json.dumps(audit)


def test_generation_detects_concurrent_change_to_another_partition():
    with GraphStore() as store:
        publish(store, [node()], "A")
        generation = store.begin_publication("registry", 1, "all", {}, replace_rule=True)
        publish(store, [node("other")], "B")
        with pytest.raises(PublicationConflict, match="rule_generation_changed"):
            store.publish_partition(completion(generation))
        assert len(store.view({"A", "B"})["nodes"]) == 2


def test_legacy_audit_cli_is_read_only_and_does_not_echo_tokens(tmp_path):
    database = tmp_path / "legacy.sqlite"
    token = "partition_" + digest(["tenant"], ["private_A"])
    with GraphStore(database) as store:
        store.begin_publication("legacy", 1, token, {})
    before = database.read_bytes()
    result = subprocess.run([sys.executable, "-m", "lightgraph.runtime.partition_audit",
                             str(database), "--fail-on-findings"], capture_output=True, text=True)
    assert result.returncode == 1, result.stderr
    assert json.loads(result.stdout)["counts"]["publication"] == 1
    assert token not in result.stdout and "private_A" not in result.stdout
    assert database.read_bytes() == before


def test_generation_and_partition_of_same_rule_cannot_publish_together():
    with GraphStore() as store:
        whole = store.begin_publication("registry", 1, "all", {}, replace_rule=True)
        part = store.begin_publication("registry", 1, "A", {})
        with pytest.raises(PublicationConflict, match="overlapping_rule_generation"):
            store.publish_partitions([completion(whole), completion(part)])
        assert not store.heads()


def test_full_generation_retraction_validates_surviving_dependency_edges():
    with GraphStore() as store:
        publish(store, [node("parent"), node("child")], "A")
        publish(store, [edge("parent", "child")], "A", rule="relation")
        objects = store.begin_publication("registry", 1, "all", {}, replace_rule=True)
        with pytest.raises(StoreError, match="dangling_edge"):
            store.publish_partition(completion(objects))
        assert store.view({"A"})["edges"]
        relations = store.begin_publication("relation", 1, "all", {}, replace_rule=True)
        store.publish_partitions([completion(objects), completion(relations)])
        assert not store.view({"A"})["nodes"]
        assert not store.view({"A"})["edges"]


def test_shared_identity_attributes_are_checked_across_query_batches():
    from lightgraph.tests.test_relative_hierarchy import rule, table as hierarchy_table, path
    spec = rule(scope="domain_global")
    spec.scope.partition_by = ["tenant"]
    rows = [path("A", "B", tenant="one"), path("A", "B", tenant="two")]
    rows[1]["l2_name"] = "incompatible label"
    with MemorySource([hierarchy_table()], {"public.items": rows}) as source, GraphStore() as store:
        result = RuleEngine(store).execute(source, spec)
        assert not result.published
        assert "identity_domain_attribute_conflict" in result.codes
        assert not store.heads()


def test_mark_for_review_invalidates_current_dependency_closure_but_retains_rules():
    from lightgraph.contracts import RuleSpec
    objects = entity_rule("objects")
    linked = RuleSpec(rule_id="linked", kind="reference", source=objects.source,
        identity=objects.identity,
        relation={"source_rule_id":"objects", "target_rule_id":"other", "source_columns":["code"], "target_columns":["code"]})
    with GraphStore() as store:
        for spec in [objects, linked, entity_rule("unrelated")]:
            store.put_rule(spec)
        old = publish(store, [node()], "A", rule="objects")
        publish(store, [], "A", rule="linked")
        publish(store, [node("independent")], "A", rule="unrelated")
        assert store.mark_rule_for_review("objects", "fd_violation") == ["linked", "objects"]
        assert {n["id"] for n in store.view({"A"})["nodes"]} == {"independent"}
        assert store.view({"A"}, publication_id=old)["nodes"]
        assert {r.rule_id for r in store.get_rules()} == {"objects", "linked", "unrelated"}
        before = store.connection.execute("SELECT COUNT(*) FROM rule_event").fetchone()[0]
        store.mark_rule_for_review("objects", "fd_violation")
        assert store.connection.execute("SELECT COUNT(*) FROM rule_event").fetchone()[0] == before
        publish(store, [node()], "A", rule="objects")
        assert {n["id"] for n in store.view({"A"})["nodes"]} == {"entity_shared", "independent"}


def test_new_rule_export_does_not_copy_historical_source_partition_tokens(tmp_path):
    import yaml
    from lightgraph.contracts import Coverage
    from lightgraph.runtime.retention import write_rules

    token = "partition_" + digest(["tenant"], ["private_A"])
    with GraphStore() as store:
        rule = partitioned_rule()
        rule.status = "validated"
        rule.dependencies.schema_ref = RuleEngine(store).schema_fingerprint(table(["tenant", "code", "label"]))
        rule.coverage = Coverage(scope="full", partitions=[token], consistent=True)
        store.put_rule(rule)
        historical = store.connection.execute("SELECT payload FROM rule").fetchone()[0]
        with MemorySource([table(["tenant", "code", "label"])], {"public.items": tenant_rows("private_A")}) as source:
            result = RuleEngine(store).execute(source, rule)
        assert result.published and result.validation.coverage.partitions == ["all"]
        cached = store.get_rules()
        target = tmp_path / "rules.yaml"
        write_rules(target, cached)
        assert token not in target.read_text()
        assert yaml.safe_load(target.read_text())[0]["coverage"] == {
            "scope": "partition", "partitions": [], "rows_scanned": None,
            "entities": None, "consistent": False}
        assert cached[0].coverage.partitions == [token]
        assert store.connection.execute("SELECT payload FROM rule").fetchone()[0] == historical
        current = cached[0].model_copy(deep=True)
        current.coverage = result.validation.coverage
        write_rules(target, [current])
        assert yaml.safe_load(target.read_text())[0]["coverage"]["partitions"] == ["all"]
        assert yaml.safe_load(target.read_text())[0]["coverage"]["consistent"] is True
