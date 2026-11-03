"""Exploration failure must leave complete core results publishable."""
from copy import deepcopy
from lightgraph.discovery.coverage import attribute_enrichment
from lightgraph.pipeline import run_pipeline
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.tests.test_agent_pipeline import client, group, input_packet, response, table
from lightgraph.tests.test_rules_engine import wide_rule






def test_enrichment_keeps_prior_aliases_and_rejects_identity_or_topology_change():
    rule=wide_rule(); candidate=deepcopy(rule)
    candidate.hierarchy.levels[0].aliases=['english']
    enriched=attribute_enrichment(rule,candidate)
    assert enriched.hierarchy.levels[0].aliases==['english']
    assert enriched.revision==2
    assert not rule.hierarchy.levels[0].aliases
    candidate.identity.key_columns=['other']
    assert attribute_enrichment(rule,candidate) is None
    candidate=deepcopy(rule);candidate.hierarchy.allow_dag=True
    assert attribute_enrichment(rule,candidate) is None
