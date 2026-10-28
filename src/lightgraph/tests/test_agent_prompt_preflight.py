"""Catalog preflight and actual Agent submission use one control envelope."""
import json
from types import SimpleNamespace

import pytest

from lightgraph.context import initial_agent_prompt_bytes
from lightgraph.contracts import canonical
from lightgraph.errors import LightGraphError
from lightgraph.tests.test_discovery_agent import Toolbox, final, input_packet, response
from lightgraph.tests.test_model_streaming import model


@pytest.mark.parametrize('steps,tool_calls',[(8,16),(1,1),(32,64)])
@pytest.mark.parametrize('response_format',['json_schema','json_object'])
def test_initial_prompt_exact_threshold_includes_runtime_controls(steps,tool_calls,response_format):
    packet=input_packet()
    toolbox=Toolbox()
    estimate=initial_agent_prompt_bytes(packet,toolbox.tool_specs(),
        max_steps=steps,max_tool_calls=tool_calls,response_format=response_format)
    requests=[]

    def handle(request):
        payload=json.loads(request.content)
        actual=len(canonical({'messages':payload['messages'],
            'tools':payload.get('tools',[]),'response_format':payload['response_format']}).encode())
        requests.append(actual)
        assert actual==estimate
        control=json.loads(payload['messages'][1]['content'])['runtime_control']
        assert control['max_turns']==steps
        assert control['remaining_tool_calls']==tool_calls
        assert control['submission_required'] is (steps==1)
        assert bool(payload.get('tools')) is (steps>1)
        assert payload['response_format']['type']==response_format
        if response_format=='json_schema':
            from lightgraph.discovery.models import ProposalBatch
            assert payload['response_format']['json_schema']['strict'] is True
            assert payload['response_format']['json_schema']['schema']==ProposalBatch.model_json_schema()
            assert json.loads(payload['messages'][1]['content'])['output_schema']['schema_ref']==(
                'response_format.json_schema.schema')
        return response(final())

    client=model(handle,stream=False,max_prompt_bytes=estimate,
        max_discovery_steps=steps,max_tool_calls=tool_calls,response_format=response_format)
    assert client.explore(packet,toolbox).proposals
    assert requests==[estimate]
    assert toolbox.closed and not toolbox.executed
    assert client.config.max_prompt_bytes==estimate
    assert client.budget.snapshot()['reserved']=={}

    requests.clear()
    too_small=model(handle,stream=False,max_prompt_bytes=estimate-1,
        max_discovery_steps=steps,max_tool_calls=tool_calls,response_format=response_format)
    rejected_tools=Toolbox()
    with pytest.raises(LightGraphError,match='model_packet_too_large'):
        too_small.explore(input_packet(),rejected_tools)
    assert not requests
    assert rejected_tools.closed and not rejected_tools.executed
    assert too_small.usage['calls']==0
    assert too_small.config.max_prompt_bytes==estimate-1
    assert too_small.budget.snapshot()['reserved']=={}


def test_catalog_window_counts_provider_schema_and_honors_compatibility_mode():
    from lightgraph.discovery.workflow import _bounded_catalog_window
    from lightgraph.tests.test_agent_pipeline import table
    primary=table('main',['opaque_a','opaque_b'])
    other=table('other',['id','caption'])
    other.columns[1].comment='Additional authorized metadata. '*115
    tools=Toolbox().tool_specs()
    configuration=SimpleNamespace(max_prompt_bytes=1000000,response_format='json_schema',
                                  max_discovery_steps=8,max_tool_calls=16)
    client=SimpleNamespace(config=configuration)
    all_tables,full=_bounded_catalog_window(primary,[primary,other],input_packet(),client,tools)
    assert len(all_tables)==2
    one={**full,'catalog':full['catalog'][:1],
         'subtask':full['subtask'].replace('2 of 2 authorized tables','1 of 2 authorized tables')}
    full_sizes={mode:initial_agent_prompt_bytes(full,tools,response_format=mode)
                for mode in ('json_schema','json_object')}
    smaller,larger=sorted(full_sizes,key=full_sizes.get)
    assert full_sizes[smaller]<full_sizes[larger]
    threshold=full_sizes[smaller]
    assert initial_agent_prompt_bytes(one,tools,response_format=larger)<threshold
    configuration.max_prompt_bytes=threshold;configuration.response_format=smaller
    selected,_=_bounded_catalog_window(primary,[primary,other],input_packet(),client,tools)
    assert selected==[primary,other]
    configuration.response_format=larger
    selected,packet=_bounded_catalog_window(primary,[primary,other],input_packet(),client,tools)
    assert selected==[primary]
    minimum=initial_agent_prompt_bytes(packet,tools,response_format=larger)
    configuration.max_prompt_bytes=minimum-1
    with pytest.raises(LightGraphError,match='model_packet_too_large'):
        _bounded_catalog_window(primary,[primary,other],input_packet(),client,tools)
