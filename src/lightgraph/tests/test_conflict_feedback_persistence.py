"""Fixed failure reasons survive a session without retaining rejected text."""
import json

from lightgraph.contracts import ExtractionGoal, SourceRef
from lightgraph.runtime.iteration import IterationLedger
from lightgraph.store import GraphStore


def test_compiler_conflict_and_supersession_diagnostics_survive_restart(tmp_path):
    source=SourceRef(source_id='s',table='items')
    path=tmp_path/'feedback.sqlite'
    with GraphStore(path) as store:
        ledger=IterationLedger(store,ExtractionGoal(),['public'])
        ledger.record_discovery_feedback([source],[
            {'code':'conflicting_rule_proposals','table':'public.items','proposal':'PRIVATE_VALUE'},
            {'code':'discovery_draft_field_diagnostic','reason_code':'object_model_supersession_mapping_loss',
             'field_path':json.dumps(['supersedes_object_ids',0]),'constraint':'contract',
             'count':2,'table':'public.items','raw':'PRIVATE_VALUE'},
        ],task='metadata_discovery')
    with GraphStore(path) as store:
        ledger=IterationLedger(store,ExtractionGoal(),['public'])
        item=ledger.enqueue(source,task='metadata_discovery',context_version='v1')
        context=ledger.context([item],[],iteration=0)
        feedback=context['prior_discovery_feedback'][0]
        assert feedback['codes']==['conflicting_rule_proposals','object_model_supersession_mapping_loss']
        assert 'supersedes_object_ids' in json.dumps(context)
        assert 'PRIVATE_VALUE' not in json.dumps(context)
    assert b'PRIVATE_VALUE' not in path.read_bytes()
