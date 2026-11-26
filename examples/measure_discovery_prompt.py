"""Offline prompt-size probe: metadata/knowledge only, no model requests.

Usage: .venv/bin/python examples/measure_discovery_prompt.py CONFIG_OR_BUNDLE [--code src]
Uses the installed discovery package. --code selects a compatible source checkout.
Output contains only numeric size/counter measurements, never source values.
"""
import argparse
import json
from pathlib import Path
import sys
from types import SimpleNamespace

parser=argparse.ArgumentParser()
parser.add_argument('bundle',type=Path)
parser.add_argument('--code',type=Path)
args=parser.parse_args()
root=args.bundle.resolve()
if args.code:
    sys.path.insert(0,str(args.code.resolve()))
sys.dont_write_bytecode=True

from lightgraph.runtime.config import load_config,open_sources
from lightgraph.documents import load_documents,DocumentCorpus
from lightgraph.discovery.engine import _source,_packet,_understanding
from lightgraph.discovery.workflow import _catalog,_bounded_catalog_window
from lightgraph.discovery.tools import ToolExecutor
from lightgraph.discovery.budget import BudgetAllocator, Cost
from lightgraph.context import initial_agent_prompt_bytes,with_discovery_runtime_control
from lightgraph.contracts import canonical
from lightgraph.discovery.proposal_session import proposal_tool_specs
from lightgraph.discovery.model_runtime import DiscoveryFormatter
from lightgraph.discovery.agent import AGENT_SYSTEM
from lightgraph.discovery.goal import build_goal_instructions
from lightgraph.discovery.llm import request_envelope_for
from lightgraph.discovery.models import TaskPacket

size=lambda value:len(canonical(value).encode())
config=load_config(root/'config.json' if root.is_dir() else root)
sources=open_sources(config, metadata_only=True)
tools=None
try:
    settings = config.resolved_model
    budget = BudgetAllocator(Cost(db_cu=config.max_db_queries, llm_calls=0, llm_tokens=None,
        seconds=config.max_run_seconds, memory_bytes=config.max_working_bytes))
    model = SimpleNamespace(config=settings, budget=budget)
    corpus=DocumentCorpus(load_documents(config.documents))
    tables=_catalog(sources[0],config.goal)
    corpus.bind_catalog(sources[0].describe())
    first=tables[0]
    docs=corpus.context_for(_source(first),first.column_names)
    packet=_packet(first,config.goal,[],docs,'T0_discovery',_understanding(first,[],{},docs))
    tools=ToolExecutor(sources[0],config.goal,model.budget,documents=corpus,
        allow_source_samples=False,allowed_tables=tables)
    window,packet=_bounded_catalog_window(first,tables,packet,model,tools.tool_specs())
    packet=TaskPacket.model_validate(packet).model_dump(mode='json',by_alias=True)
    tools.begin_context([_source(first)])
    formatter=DiscoveryFormatter(packet,tools)
    system=AGENT_SYSTEM+'\n\n'+build_goal_instructions(config.goal.model_dump(mode='json'),stage='T0_discovery')
    system+='\nCurrent stage: T0_discovery\nSubtask: '+packet['subtask']
    msgs=[{'role':'system','content':system},{'role':'user','content':'task'}]
    print(json.dumps({'table_count':len(tables),'initial_prompt_bytes':initial_agent_prompt_bytes(
        packet,tools.tool_specs(),max_steps=model.config.max_discovery_steps,max_tool_calls=model.config.max_tool_calls),
        'prompt_limit_bytes':model.config.max_prompt_bytes}))
    sequence=[('get_metadata',table) for table in tables]
    sequence += [('search_knowledge',table) for table in tables[:4]]
    for index,(name,table) in enumerate(sequence):
        arguments={'table':f'{table.source_id}:{table.qualified_name}'}
        result=tools.execute(name,arguments)
        if result.get('status')!='ok':raise RuntimeError('offline_tool_failed')
        msgs.extend([{'role':'assistant','content':None,'tool_calls':[{'id':str(index),'type':'function',
            'function':{'name':name,'arguments':canonical(arguments)}}]},
            {'role':'tool','tool_call_id':str(index),'content':canonical(result)}])
        formatted=formatter.format(msgs)
        formatted=with_discovery_runtime_control(formatted,turn=index+2,max_turns=model.config.max_discovery_steps,
            remaining_tool_calls=model.config.max_tool_calls-index-1)
        request_bytes=size(request_envelope_for(formatted,[*tools.tool_specs(),*proposal_tool_specs('T0_discovery')],None))
        print(json.dumps({'completed_tools':index+1,'tool_result_bytes':size(result),
            'attached_context_bytes':size(result.get('context',{})),
            'formatted_request_bytes_before_dynamic_window':request_bytes,
            'exceeds_prompt_limit':int(request_bytes>model.config.max_prompt_bytes) if model.config.max_prompt_bytes else None}))
    assert budget.used.llm_calls==0
    print(json.dumps({'model_calls':budget.used.llm_calls,'database_queries':sum(s.stats.queries for s in sources)}))
finally:
    if tools:tools.close()
    for source in sources:source.close()
