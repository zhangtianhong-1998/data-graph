"""Exercise the actual AgentScope formatter/request/trace boundary offline."""
import json
from lightgraph.runtime.trace import bind_trace, RunTrace
from lightgraph.tests.test_discovery_agent import Toolbox, final, input_packet, response
from lightgraph.tests.test_model_streaming import model


def test_proactive_compaction_keeps_drafts_and_full_trace_below_a_large_hard_cap(tmp_path):
    class Source(Toolbox):
        def execute(self,name,args):
            self.executed.append(name)
            old=len(self.executed)==1
            return {'status':'ok','operator':name,'probe_id':f'probe_{len(self.executed):04d}',
                'data':{'columns':['字段名'],
                    'rows':[[('OLD_TRACE_DETAIL'+'x'*80000) if old else 'CURRENT_DETAIL']],
                    'complete':False},'coverage':{'result_complete':False,'truncated':True}}
    requests=[]
    def handle(request):
        body=json.loads(request.content);requests.append(body)
        if len(requests) in (1,3):return response(tool='get_metadata',args={'page':len(requests)})
        if len(requests)==2:
            assert 'OLD_TRACE_DETAIL' in json.dumps(body)
            proposal=final()['proposals'][0]
            return response(tool='submit_column_group',args={k:v for k,v in proposal.items() if k!='kind'})
        wire=json.dumps(body)
        assert 'OLD_TRACE_DETAIL' not in wire and 'CURRENT_DETAIL' in wire and 'draft_1' in wire
        assert 'get_metadata' in [t['function']['name'] for t in body['tools']]
        calls=[c['id'] for m in body['messages'] for c in m.get('tool_calls',[])]
        replies=[m['tool_call_id'] for m in body['messages'] if m['role']=='tool']
        assert calls==replies
        return response(tool='finish_discovery',args={'pending':False})
    trace_path=tmp_path/'trace.jsonl'
    client=model(handle,stream=False,max_prompt_tokens=1048576,max_discovery_steps=8)
    source=Source()
    with bind_trace(RunTrace(trace_path)):
        batch=client.explore(input_packet(),source)
    events=[(e['kind'],e['data']) for line in trace_path.read_text(encoding='utf-8').splitlines()
            for e in [json.loads(line)]]
    assert len(batch.proposals)==1 and not batch._runtime_pending_codes
    assert client.usage['calls']==4 and client.usage['failures']==0
    assert client.usage['context_compaction_compressed_results']>=1
    assert not client.usage.get('prompt_packets_rejected')
    compacted=[data for kind,data in events if kind=='context_compaction' and data['compressed_results']]
    assert compacted and compacted[-1]['after_tokens']<compacted[-1]['before_tokens']
    # Request projections change; original tool events are still exportable.
    assert any(kind=='tool_result' and 'OLD_TRACE_DETAIL' in json.dumps(data) for kind,data in events)
    assert source.closed and client.budget.snapshot()['reserved']=={}
