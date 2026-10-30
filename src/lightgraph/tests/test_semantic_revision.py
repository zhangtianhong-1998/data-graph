"""A new structure gets one review; renaming, dropping conditions and voting do not."""
import json
from types import SimpleNamespace
import pytest
from lightgraph.contracts import ExtractionGoal
from lightgraph.discovery.engine import _column_id
from lightgraph.discovery.models import ProposalBatch, DiscoveryResult
from lightgraph.discovery.semantics import SemanticReview
from lightgraph.discovery.workflow import _compile_candidates
from lightgraph.tests.test_agent_pipeline import table


def setup_batch():
    t=table('items',['key','parent','label'])
    cols={c.name:_column_id(t,c.name) for c in t.columns}
    proposal={'kind':'long_hierarchy','claim':{'group_id':'h','key_column':cols['key'],
        'parent_column':cols['parent'],'label_column':cols['label']},'evidence_cited':[cols['key']]}
    batch=ProposalBatch.model_validate({'task':'T0_discovery','proposals':[proposal]})
    packet={'task':'T0_discovery','metadata_digest':{'columns':[{'id':v,'name':k} for k,v in cols.items()]}}
    return t,batch,packet


@pytest.mark.parametrize('repair',['reverse','same','rename','drop_condition','alter_condition'])
def test_only_changed_structure_with_original_obligations_can_compile(repair):
    t,batch,packet=setup_batch(); result=DiscoveryResult();calls=[];review_packets=[]
    class Model:
        config=SimpleNamespace(max_discovery_steps=3,max_tool_calls=2)
        def complete(self,task,p,response_model):
            calls.append(task);review_packets.append(p)
            reference=p['metadata_digest']['columns'][0]['id']
            original=len(review_packets)==1
            conditions=[{'state':'unknown' if original else 'satisfied','evidence_refs':[reference],
                'explanation':'EPHEMERAL original condition'}]
            if not original:
                assert p['semantic_revision_guard']['conditions'][0]['condition']=='EPHEMERAL original condition'
                conditions[0]['condition_ref']='condition_1'
                if repair=='drop_condition':conditions=[]
                if repair=='alter_condition':conditions[0]['explanation']='easier condition'
            return SemanticReview(proposition_ref=p['semantic_proposition_ref'],decision='supported',
                basis='data_inference',evidence=[{'reference':reference,'stance':'supports'}],conditions=conditions)
        def explore(self,p,tools,**kw):
            calls.append(p['task']);assert p['iteration_context']['semantic_revision_attempt']==1
            alternative=batch.model_copy(deep=True);alternative.task='T5_revision'
            claim=alternative.proposals[0].claim
            if repair=='rename':claim.group_id='renamed'
            elif repair!='same':claim.key_column,claim.parent_column=claim.parent_column,claim.key_column
            return alternative
    class Tools:
        def context_documents(self):return []
        def close(self):pass
    rules,*_=_compile_candidates(batch,t,ExtractionGoal(),[],result,model=Model(),packet=packet,
        observations=None,tool_factory=Tools)
    assert calls.count('T5_revision')==1
    assert calls.count('semantic_review')==(1 if repair in ('same','rename') else 2)
    assert bool(rules)==(repair=='reverse')
    assert len(result.pending_candidates)==1
    serialized=result.model_dump_json()
    assert 'EPHEMERAL' not in serialized and 'easier condition' not in serialized
    if rules:
        cid=result.pending_candidates[0].candidate_id
        assert result.resolved_candidate_ids==[cid]
        assert result.candidate_replacement_rules[cid]==[rules[0].rule_id]
    else:assert not result.resolved_candidate_ids
