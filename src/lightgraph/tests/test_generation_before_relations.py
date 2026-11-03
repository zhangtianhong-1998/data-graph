"""A known cross-rule naming conflict must be repaired before optional T2 work."""
from types import SimpleNamespace

from lightgraph.discovery.models import DiscoveryResult
from lightgraph.documents import load_document
from lightgraph.pipeline import run_pipeline
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.tests.test_agent_pipeline import table
from lightgraph.tests.test_concept_identity_domains import shared_rules


def test_mapping_conflict_keeps_table_pending_after_forced_submission():
    from lightgraph.discovery.models import ProposalBatch
    from lightgraph.discovery.workflow import _record_submission_state
    batch=ProposalBatch(task='T1_table_discovery',abstain=True)
    batch._runtime_pending_codes=['object_model_mapping_conflict']
    result=DiscoveryResult()
    _record_submission_state(result,table('metric',['code','name']),batch)
    assert result.issues==[{'table':'public.metric','code':'object_model_mapping_conflict','status':'deferred'}]
    assert result.stats['discovery_pending_submissions']==1
