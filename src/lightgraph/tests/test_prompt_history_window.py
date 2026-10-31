"""Long source histories must still reach submission without broken tool pairs."""
import json
from copy import deepcopy
import pytest

from lightgraph.discovery.prompt_window import fit_source_history, prompt_bytes
from lightgraph.tests.test_discovery_agent import Toolbox, final, input_packet, response
from lightgraph.tests.test_model_streaming import model


def exchange(names, width=3000):
    return [{'role':'assistant','content':None,'tool_calls':[
        {'id':name,'type':'function','function':{'name':'get_metadata','arguments':'{}'}} for name in names]},
        *[{'role':'tool','tool_call_id':name,'content':name+'x'*width} for name in names]]


def test_complete_batches_are_evicted_but_local_feedback_and_latest_evidence_stay():
    local=exchange(['draft'],10)
    local[0]['tool_calls'][0]['function']['name']='submit_column_group'
    messages=[{'role':'system','content':'policy'},{'role':'user','content':'fixed task'},
        *exchange(['old_a','old_b']),*local,*exchange(['middle']),*exchange(['latest'])]
    original=deepcopy(messages)
    tools=Toolbox().tool_specs()
    fitted,removed=fit_source_history(messages,tools,'json_schema',5000,{'submit_column_group'})
    assert removed==2 and prompt_bytes(fitted,tools,'json_schema')<=5000
    assert [m['tool_call_id'] for m in fitted if m['role']=='tool']==['draft','latest']
    assert local[0] in fitted and local[1] in fitted
    assert messages==original
    ids=[c['id'] for m in fitted for c in m.get('tool_calls',[])]
    assert ids==[m['tool_call_id'] for m in fitted if m['role']=='tool']
    # An incomplete batch is never partially deleted to manufacture a fit.
    broken=[*messages[:2],*exchange(['missing'])[:1],*exchange(['latest'])]
    fitted,removed=fit_source_history(broken,tools,'json_schema',1,set())
    assert not removed and fitted==broken


@pytest.mark.parametrize('repair_feedback',[False,True])
def test_history_pressure_finishes_existing_draft_with_no_extra_calls(capsys,caplog,repair_feedback):
    class LargeTools(Toolbox):
        def execute(self,name,args):
            self.executed.append(name)
            return {'status':'ok','operator':name,'probe_id':f'probe_{len(self.executed):04d}',
                'data':{'text':('OLD_EVIDENCE' if len(self.executed)==1 else 'LATEST_EVIDENCE')+'x'*33000}}
    seen=[]
    limit=90000
    def handle(request):
        body=json.loads(request.content)
        seen.append(body)
        assert prompt_bytes(body['messages'],body.get('tools'),'json_schema')<=limit
        if len(seen)==1:return response(tool='get_metadata',args={'page':1})
        if len(seen)==2:
            proposal=final()['proposals'][0]
            return response(tool='submit_column_group',args={k:v for k,v in proposal.items() if k!='kind'})
        if len(seen)==3:return response(tool='get_metadata',args={'page':2})
        control=json.loads(body['messages'][1]['content'])['runtime_control']
        assert control['submission_reason']=='context_window_pressure'
        assert all(t['function']['name'].startswith(('submit_','declare_','finish_')) for t in body['tools'])
        wire=json.dumps(body)
        assert 'OLD_EVIDENCE' not in wire and 'LATEST_EVIDENCE' in wire
        assert 'draft_1' in wire
        if repair_feedback and len(seen)==4:
            proposal=final(role='not_a_role')['proposals'][0]
            return response(tool='submit_column_group',args={k:v for k,v in proposal.items() if k!='kind'})
        if repair_feedback and len(seen)==5:
            assert 'diagnostics' in wire and 'invalid_tool_arguments' in wire
            proposal=final()['proposals'][0]
            error=json.loads(body['messages'][-1]['content'])
            return response(tool='submit_column_group',args={**{k:v for k,v in proposal.items() if k!='kind'},
                'resolves_rejections':[error['rejection_id']]})
        return response(tool='finish_discovery',args={'pending':False})
    client=model(handle,stream=False,max_prompt_bytes=limit,max_discovery_steps=8)
    tools=LargeTools()
    batch=client.explore(input_packet(),tools)
    assert len(batch.proposals)==1 and not batch._runtime_pending_codes
    assert len(seen)==client.usage['calls']==(6 if repair_feedback else 4)
    assert client.usage['prompt_pressure_turns']==(3 if repair_feedback else 1)
    assert client.usage['prompt_history_exchanges_omitted']==(3 if repair_feedback else 1)
    assert client.usage['prompt_peak_before_window_bytes']>limit
    assert client.usage['prompt_peak_after_window_bytes']<=limit
    assert client.budget.snapshot()['reserved']=={} and tools.closed
    assert 'EVIDENCE' not in json.dumps(client.usage)+capsys.readouterr().out+caplog.text
