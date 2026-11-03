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
