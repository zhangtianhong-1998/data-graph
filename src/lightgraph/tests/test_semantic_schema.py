"""Project vocabulary is extensible, but its contracts and output permission are not optional."""
import json

import jsonschema
import pytest

from lightgraph.contracts import ExtractionGoal, RuleSpec, GraphFact, SupportSpec, canonical
from lightgraph.errors import LightGraphError
from lightgraph.semantic_schema import SemanticSchema, EntityTypeDefinition
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore, PersistenceViolation
from lightgraph.rules import RuleEngine
from lightgraph.presentation import export_graph
from lightgraph.presentation.importer import import_graph
from lightgraph.presentation.schema import snapshot_schema
from lightgraph.tests.test_rules_engine import table, completion


def vocabulary():
    return SemanticSchema.model_validate({
        "entity_types": [{"name":"Person", "description":"A person represented in the source."}],
        "properties": [{"name":"birth_year", "description":"Calendar year of birth.",
                        "value_type":"integer", "entity_kinds":["Person"]}],
    })


def person_rule(schema=None):
    return RuleSpec(rule_id="persons", kind="column_group_entity", source={"source_id":"s", "table":"items"},
        identity={"key_columns":["id"], "entity_kind":"Person"},
        attributes={"single_valued":{"label":"name", "birth_year":"year"}},
        semantic_schema=schema or SemanticSchema())


def source():
    return MemorySource([table(["id", "name", "year", "private"])], {
        "public.items":[{"id":"P1", "name":"Alice", "year":1990, "private":"hidden"}]})


def test_goal_declared_type_and_typed_property_persist_and_reopen(tmp_path):
    schema = vocabulary()
    # The explicit goal's Person name supplies the type declaration when the
    # user only fills in the new property's definition.
    schema.entity_types = []
    goal = ExtractionGoal(entity_kinds=["Person"], semantic_schema=schema)
    database = tmp_path / "graph.sqlite"
    with GraphStore(database) as store, source() as adapter:
        store.configure_goal(goal, 3)
        outcome = RuleEngine(store).execute(adapter, person_rule())
        assert outcome.published, outcome.codes
        assert store.connection.execute("SELECT DISTINCT goal_revision FROM semantic_catalog").fetchall()[0][0] == 3
    with GraphStore(database) as store:
        graph = store.view({"public"})
        assert graph["nodes"][0]["kind"] == "Person"
        assert graph["nodes"][0]["properties"]["birth_year"] == 1990
        assert graph["semantic_schema"]["entity_types"][0]["name"] == "Person"
        assert store.policy.property_definitions["birth_year"].value_type == "integer"


def test_automatic_vocabulary_requires_definitions_and_does_not_admit_any_string():
    with GraphStore() as store:
        store.configure_goal(ExtractionGoal(entity_kinds=[], relation_kinds=[]))
        with source() as adapter:
            missing = RuleEngine(store).execute(adapter, person_rule())
        assert not missing.published and "unregistered_node_kind" in missing.codes
        assert store.connection.execute("SELECT COUNT(*) FROM semantic_catalog").fetchone()[0] == 0
        with source() as adapter:
            defined = RuleEngine(store).execute(adapter, person_rule(vocabulary()))
        assert defined.published, defined.codes


@pytest.mark.parametrize("property_name,value_type", [("level_no", "integer"), ("label", "string"), ("source_ref", "string")])
def test_dynamic_schema_cannot_override_builtin_property_contract(property_name, value_type):
    schema = vocabulary()
    schema.properties[0].name = property_name
    schema.properties[0].value_type = value_type
    with GraphStore() as store:
        with pytest.raises(PersistenceViolation, match="builtin_property_definition_forbidden"):
            store.configure_goal(ExtractionGoal(entity_kinds=["Person"], semantic_schema=schema))
        with pytest.raises(PersistenceViolation, match="builtin_property_definition_forbidden"):
            store.register_import_schema(schema)
        with pytest.raises(PersistenceViolation, match="builtin_property_definition_forbidden"):
            snapshot_schema(schema.model_dump())
        assert not store.policy.property_definitions
        assert "Person" not in store.policy.kinds["business"]
        assert store.connection.execute("SELECT COUNT(*) FROM semantic_catalog").fetchone()[0] == 0


def test_custom_property_and_builtin_properties_export_with_distinct_contracts(tmp_path):
    with GraphStore() as store, source() as adapter:
        store.configure_goal(ExtractionGoal(entity_kinds=[], relation_kinds=[]))
        assert RuleEngine(store).execute(adapter, person_rule(vocabulary())).published
        graph = export_graph(store, tmp_path, scopes={"public"})
        exported_schema = json.loads((tmp_path / "graph.schema.json").read_text())
        jsonschema.validate(graph, exported_schema)
        properties = {item["name"] for item in graph["semantic_schema"]["properties"]}
        assert properties == {"birth_year"}
        graph["facts"] = [dict(fact, payload={**fact["payload"], "value": "not-an-integer"})
            if fact["fact_kind"] == "PropertyValue" and fact["payload"]["name"] == "birth_year" else fact
            for fact in graph["facts"]]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(graph, exported_schema)


@pytest.mark.parametrize("relation_name", ["parent_of", "same_entity"])
def test_dynamic_schema_cannot_silently_narrow_builtin_relation_endpoints(relation_name):
    schema = SemanticSchema.model_validate({
        "entity_types": [{"name": "Person", "description": "A person."}],
        "relations": [{"name": relation_name, "description": "A user-specified relation.",
                       "source_kinds": ["Person"], "target_kinds": ["Person"]}],
    })
    with GraphStore() as store:
        with pytest.raises(PersistenceViolation, match="builtin_relation_definition_forbidden"):
            store.configure_goal(ExtractionGoal(entity_kinds=[], relation_kinds=[], semantic_schema=schema))
        with pytest.raises(PersistenceViolation, match="builtin_relation_definition_forbidden"):
            snapshot_schema(schema.model_dump())
        assert not store.policy.relation_definitions
        assert "Person" not in store.policy.kinds["business"]
        assert store.connection.execute("SELECT COUNT(*) FROM semantic_catalog").fetchone()[0] == 0


@pytest.mark.parametrize("restriction", ["excluded_columns", "evidence_only_columns"])
def test_new_property_cannot_launder_a_restricted_source_column(restriction):
    goal = ExtractionGoal(entity_kinds=[], relation_kinds=[], **{restriction:["private"]})
    rule = person_rule(vocabulary())
    rule.attributes.single_valued["birth_year"] = "private"
    with GraphStore() as store, source() as adapter:
        store.configure_goal(goal)
        outcome = RuleEngine(store).execute(adapter, rule)
        assert not outcome.published and not outcome.prepared
        assert any("column_projected" in code for code in outcome.codes)
        assert adapter.stats.queries == 0
        assert store.connection.execute("SELECT COUNT(*) FROM semantic_catalog").fetchone()[0] == 0
        assert "Person" not in store.policy.kinds["business"]
        assert not store.view({"public"}, include_candidates=True)["nodes"]


def test_failed_typed_output_does_not_activate_proposed_schema():
    with GraphStore() as store:
        store.configure_goal(ExtractionGoal(entity_kinds=[], relation_kinds=[]))
        with MemorySource([table(["id", "name", "year"])], {"public.items":[{"id":"1", "name":"Alice", "year":"not an integer"}]}) as adapter:
            outcome = RuleEngine(store).execute(adapter, person_rule(vocabulary()))
        assert not outcome.published
        assert "semantic_property_value_type_mismatch" in outcome.codes
        assert store.connection.execute("SELECT COUNT(*) FROM semantic_catalog").fetchone()[0] == 0
        assert "Person" not in store.policy.kinds["business"]


def test_dynamic_schema_export_import_and_json_schema(tmp_path):
    output = tmp_path / "export"
    with GraphStore() as store, source() as adapter:
        store.configure_goal(ExtractionGoal(entity_kinds=[], relation_kinds=[]))
        assert RuleEngine(store).execute(adapter, person_rule(vocabulary())).published
        graph = export_graph(store, output, scopes={"public"})
        jsonschema.validate(graph, snapshot_schema(graph["semantic_schema"]))
    imported = tmp_path / "import.sqlite"
    assert import_graph(output / "graph.json", imported)["nodes"] == 1
    with GraphStore(imported) as store:
        assert store.view({"public"})["nodes"][0]["properties"]["birth_year"] == 1990
    # Omitting the schema must not cause the importer to accept arbitrary names.
    graph.pop("semantic_schema")
    (output / "no-schema.json").write_text(json.dumps(graph))
    with pytest.raises(PersistenceViolation, match="unregistered_node_kind|property_not_in_output_contract"):
        import_graph(output / "no-schema.json", tmp_path / "invalid.sqlite")


@pytest.mark.parametrize("failure", ["missing_support", "endpoint", "after_publish", "cancel_after_publish"])
def test_failed_import_rolls_back_vocabulary_facts_and_publication(tmp_path, monkeypatch, failure):
    import copy
    with GraphStore() as store, source() as adapter:
        store.configure_goal(ExtractionGoal(entity_kinds=[], relation_kinds=[]))
        assert RuleEngine(store).execute(adapter, person_rule(vocabulary())).published
        original = export_graph(store, tmp_path / "export", scopes={"public"})
    data = copy.deepcopy(original)
    if failure == "missing_support":
        data["supports"] = []
    if failure == "endpoint":
        removed = {fact["id"] for fact in data["facts"] if fact["fact_kind"] == "NodeExistence"}
        data["facts"] = [fact for fact in data["facts"] if fact["id"] not in removed]
        data["supports"] = [support for support in data["supports"] if support["fact_id"] not in removed]
    if failure in {"after_publish", "cancel_after_publish"}:
        publish = GraphStore.publish_partition
        def fail_after_publish(self, completed):
            publish(self, completed)
            if failure == "cancel_after_publish":
                raise KeyboardInterrupt()
            raise LightGraphError("injected_import_failure")
        monkeypatch.setattr(GraphStore, "publish_partition", fail_after_publish)
    target = tmp_path / "target.sqlite"
    input_path = tmp_path / "snapshot.json"
    input_path.write_text(json.dumps(data))
    expected = KeyboardInterrupt if failure == "cancel_after_publish" else LightGraphError
    with pytest.raises(expected):
        import_graph(input_path, target)
    with GraphStore(target) as store:
        assert not store.heads()
        assert "Person" not in store.policy.kinds["business"]
        for name in ("semantic_catalog", "graph_fact", "fact_support", "publication", "publication_support"):
            assert store.connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] == 0
    monkeypatch.undo()
    original["semantic_schema"]["entity_types"][0]["description"] = "Corrected import definition."
    input_path.write_text(json.dumps(original))
    assert import_graph(input_path, target)["nodes"] == 1


def test_import_transaction_restores_in_memory_policy_on_failure():
    with GraphStore() as store:
        prior = store.policy
        with pytest.raises(LightGraphError, match="failed_import"):
            with store.import_transaction():
                store.register_import_schema(vocabulary())
                raise LightGraphError("failed_import")
        assert store.policy is prior
        assert store.connection.execute("SELECT COUNT(*) FROM semantic_catalog").fetchone()[0] == 0


@pytest.mark.parametrize("status", ["candidate", "accepted"])
def test_dynamic_value_type_and_endpoint_type_checks_apply_to_all_statuses(status):
    schema = vocabulary()
    schema.entity_types.append(EntityTypeDefinition(name="Organization", description="An organization."))
    schema = SemanticSchema.model_validate(schema.model_dump())
    with GraphStore() as store:
        store.register_import_schema(schema)
        publication = store.begin_publication("manual", 1, "all", {})
        support = SupportSpec(origin="user_input", source_binding="manual", scope_ref="public", partition_id="all",
                              rule_id="manual", rule_revision=1, status=status)
        with pytest.raises(PersistenceViolation, match="semantic_property_value_type_mismatch"):
            store.stage_fact(publication, GraphFact(fact_kind="PropertyValue",
                payload={"entity_id":"one", "name":"birth_year", "value":True}), support)
        store.stage_fact(publication, GraphFact(fact_kind="NodeExistence",
            payload={"entity_id":"one", "kind":"Organization", "layer":"business"}), support)
        store.stage_fact(publication, GraphFact(fact_kind="PropertyValue",
            payload={"entity_id":"one", "name":"birth_year", "value":1990}), support)
        with pytest.raises(PersistenceViolation, match="property_entity_kind_mismatch"):
            store.publish_partition(completion(publication))


def test_incompatible_schema_revision_is_rejected_without_widening_policy():
    first = ExtractionGoal(entity_kinds=["Person"], semantic_schema=vocabulary())
    second = first.model_copy(deep=True)
    second.semantic_schema.properties[0].value_type = "string"
    with GraphStore() as store:
        store.configure_goal(first, 1)
        with pytest.raises(LightGraphError, match="semantic_definition_conflict"):
            store.configure_goal(second, 2)
        assert store.policy.property_definitions["birth_year"].value_type == "integer"
        assert store.connection.execute("SELECT COUNT(*) FROM semantic_catalog").fetchone()[0] == 1


def test_old_rule_payload_without_new_optional_contracts_remains_immutable():
    from lightgraph.tests.test_rules_engine import entity_rule
    rule = entity_rule()
    payload = rule.model_dump(mode="json", by_alias=True, exclude={"status", "semantic_schema"})
    for key in ("origin", "semantic_decision", "semantic_status", "reviewed_context_refs", "refuting_refs", "conditions_satisfied"):
        payload["evidence"].pop(key, None)
    serialized = canonical(payload)
    with GraphStore() as store:
        store.connection.execute("INSERT INTO rule VALUES(?,?,?,'draft','valid')", (rule.rule_id, rule.revision, serialized))
        store.put_rule(store.get_rules()[0])
        assert store.connection.execute("SELECT payload FROM rule").fetchone()[0] == serialized
