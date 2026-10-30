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
            'tools':payload.get('tools',[])}).encode())
        requests.append(actual)
        assert actual==estimate
        control=json.loads(payload['messages'][1]['content'])['runtime_control']
        assert control['max_turns']==steps
        assert control['remaining_tool_calls']==tool_calls
        assert control['submission_required'] is (steps==1)
        assert payload.get('tools')
        if steps==1:
            assert all(item['function']['name'].startswith(('submit_', 'declare_', 'finish_')) for item in payload['tools'])
        assert 'response_format' not in payload
        assert json.loads(payload['messages'][1]['content'])['output_schema']['type']=='object'
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
    assert full_sizes['json_schema']==full_sizes['json_object']
    configuration.max_prompt_bytes=full_sizes['json_schema']
    selected,_=_bounded_catalog_window(primary,[primary,other],input_packet(),client,tools)
    assert selected==[primary]  # Optional tables leave space for later tool replies.
    configuration.max_prompt_bytes=full_sizes['json_schema']*2
    selected,_=_bounded_catalog_window(primary,[primary,other],input_packet(),client,tools)
    assert selected==[primary,other]
    threshold=initial_agent_prompt_bytes(one,tools)
    configuration.max_prompt_bytes=threshold
    selected,packet=_bounded_catalog_window(primary,[primary,other],input_packet(),client,tools)
    assert selected==[primary]
    configuration.max_prompt_bytes=threshold-1
    with pytest.raises(LightGraphError,match='model_packet_too_large'):
        _bounded_catalog_window(primary,[primary,other],input_packet(),client,tools)

    # Optional metadata must also fit the actual unreserved token allowance,
    # even if the configured prompt byte ceiling is substantially larger.
    from lightgraph.discovery.budget import BudgetAllocator,Cost
    from lightgraph.errors import BudgetExceeded
    configuration.max_prompt_bytes=1000000
    configuration.max_output_tokens=4096
    client.budget=BudgetAllocator(Cost(llm_tokens=threshold+4096+1000,seconds=60))
    client.budget.reserve('other_work',Cost(llm_tokens=1000))
    selected,_=_bounded_catalog_window(primary,[primary,other],input_packet(),client,tools)
    assert selected==[primary]
    client.budget.charge(Cost(llm_tokens=1))
    with pytest.raises(BudgetExceeded,match='llm_tokens_budget_exhausted'):
        _bounded_catalog_window(primary,[primary,other],input_packet(),client,tools)
