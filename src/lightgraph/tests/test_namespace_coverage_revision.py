"""Missing source scope must be reviewed and replace, not duplicate, a rule."""
import json

import pytest

from lightgraph.pipeline import run_pipeline
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import client, group, input_packet, response, table
from lightgraph.tests.test_discovery_followup_priority import hierarchy, supported






def test_scope_refinement_cannot_remove_scope_change_keys_or_change_predicate():
    from lightgraph.discovery.coverage import namespace_refinement
    from lightgraph.tests.test_rules_engine import wide_rule
    old=wide_rule()
    new=old.model_copy(deep=True)
    new.scope.namespaces=[*old.scope.namespaces,'region']
    new.identity.key_columns=['region',*old.identity.key_columns]
    assert namespace_refinement(old,new) is not None
    assert namespace_refinement(new,old) is None
    changed=new.model_copy(deep=True);changed.identity.key_columns[-1]='different_key'
    assert namespace_refinement(old,changed) is None
    changed=new.model_copy(deep=True);changed.scope.predicate={'region':'one'}
    assert namespace_refinement(old,changed) is None
