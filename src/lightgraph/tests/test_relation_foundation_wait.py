"""Missing hierarchy endpoints defer their own work, not independent tables."""
from types import SimpleNamespace

from lightgraph.discovery.models import DiscoveryResult
from lightgraph.pipeline import _missing_hierarchy_foundation, run_pipeline
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import table
from lightgraph.tests.test_resume_pipeline import entity, isolate_discovery, model, result_for


def remembered():
    return {'object_id':'path','status':'candidate','basis':'agent_field_model',
        'positions':[{'position':i,'code':f'code_{i}','label':f'label_{i}'} for i in (1,2)]}


def test_only_missing_local_foundations_wait_and_ready_endpoint_releases_them():
    current=table('paths',['code','name'])
    knowledge=SimpleNamespace(current_objects=lambda t:[remembered()])
    assert _missing_hierarchy_foundation(current,[entity('other','other')],knowledge)
    assert not _missing_hierarchy_foundation(current,[entity('path','paths')],knowledge)
    knowledge.current_objects=lambda t:[]
    assert not _missing_hierarchy_foundation(current,[],knowledge)
