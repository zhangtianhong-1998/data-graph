"""Offline regressions for open discovery work and committed graph export."""
import json
import pytest

from lightgraph.errors import LightGraphError
from lightgraph.pipeline import run_pipeline
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import client,group,input_packet,response,table














def test_rule_identity_includes_kind_and_namespace_without_attribute_id_churn():
    from lightgraph.discovery.engine import _logical_entity_rule_id
    t=table('objects',['tenant','code','name'])
    first=_logical_entity_rule_id(t,'column_group_entity','Metric',['tenant','code'],['tenant'])
    assert first!=_logical_entity_rule_id(t,'column_group_entity','Measure',['tenant','code'],['tenant'])
    assert first!=_logical_entity_rule_id(t,'column_group_entity','Metric',['tenant','code'],[])
    assert first==_logical_entity_rule_id(t,'column_group_entity','Metric',['tenant','code'],['tenant'])


def test_superseded_relation_task_reactivates_when_endpoint_set_returns():
    from lightgraph.contracts import ExtractionGoal,SourceRef
    from lightgraph.runtime.iteration import IterationLedger
    with GraphStore() as store:
        ledger=IterationLedger(store,ExtractionGoal(),['public'])
        source=SourceRef(source_id='s',table='t')
        a=ledger.enqueue(source,task='relation_discovery',context_version='endpoints_A')
        ledger.transition(a,'running');ledger.transition(a,'resolved')
        b=ledger.enqueue(source,task='relation_discovery',context_version='endpoints_B')
        ledger.supersede_previous(b);ledger.transition(b,'running');ledger.transition(b,'resolved')
        returned=ledger.enqueue(source,task='relation_discovery',context_version='endpoints_A')
        assert returned.state=='pending'
        ledger.supersede_previous(returned);ledger.transition(returned,'running')
        assert returned.attempts==2
