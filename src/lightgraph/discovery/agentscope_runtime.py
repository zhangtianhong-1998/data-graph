"""AgentScope runtime adapters; no sessions, trace exporters or source snapshots.

ReActAgent owns the loop, Toolkit owns registered tool dispatch, and the existing
bounded transport owns HTTP/SSE parsing. Only complete model responses reach the
framework, so an interrupted argument stream can never execute a query.
"""
from __future__ import annotations

import asyncio
import json
from collections import OrderedDict

from agentscope import _config as framework_config
from agentscope.agent import ReActAgent
from agentscope.formatter import FormatterBase
from agentscope.memory import InMemoryMemory
from agentscope.message import Msg
from agentscope.model import ChatModelBase, ChatResponse

from lightgraph.context import packet_call_context, bounded_discovery_packet
from lightgraph.contracts import canonical
from lightgraph.errors import BudgetExceeded, LightGraphError
from .models import ProposalBatch


def _check_runtime_policy() -> None:
    # AgentScope tracing includes prompts and tool outputs. Do not let embedding
    # this module in an app that enabled tracing silently export source samples.
    if framework_config.trace_enabled:
        raise LightGraphError('agent_source_tracing_forbidden')


class EphemeralMemory(InMemoryMemory):
    """Framework memory may hold samples during one run, but cannot be saved."""

    def state_dict(self) -> dict:
        raise LightGraphError('agent_source_persistence_forbidden')

    def load_state_dict(self, state_dict: dict, strict: bool=True) -> None:
        raise LightGraphError('agent_source_persistence_forbidden')


class DiscoveryFormatter(FormatterBase):
    """Text/tool-only formatter, with fresh scoped documents on every model call.

    Avoid the generic multimodal formatter's implicit file/network processing.
    Preserve the native tool-call protocol and never flatten tool results into
    system instructions.
    """

    def __init__(self, packet: dict, executor):
        self.packet=packet
        self.executor=executor

    async def format(self, msgs: list[Msg]) -> list[dict]:
        _check_runtime_policy()
        if callable(getattr(self.executor,'context_documents',None)):
            self.packet['document_snippets']=self.executor.context_documents()
            self.packet['document_refs']=self.executor.document_refs()
            self.packet['context_tables']=self.executor.context_tables()
        visible=bounded_discovery_packet(self.packet)
        context=packet_call_context(visible,visible['task'],
                                   output_contract={'schema_ref':'output_schema'})
        packet_text=canonical({'task':visible['task'],'input':visible,
            'call_context':context,'output_schema':ProposalBatch.model_json_schema()})
        output=[]
        for msg in msgs:
            if msg.role=='user':
                output.append({'role':'user','content':packet_text})
            elif msg.has_content_blocks('tool_result'):
                for block in msg.get_content_blocks('tool_result'):
                    content=block['output']
                    if not isinstance(content,list) or any(b.get('type')!='text' for b in content):
                        raise LightGraphError('agent_nontext_tool_response')
                    output.append({'role':'tool','tool_call_id':block['id'],
                                   'content':''.join(b['text'] for b in content)})
            elif msg.has_content_blocks('tool_use'):
                calls=[]
                for block in msg.get_content_blocks('tool_use'):
                    args=block['input']
                    calls.append({'id':block['id'],'type':'function','function':{
                        'name':block['name'],'arguments':args if isinstance(args,str) else canonical(args)}})
                output.append({'role':'assistant','content':None,'tool_calls':calls})
            else:
                output.append({'role':msg.role,'content':msg.get_text_content() or ''})
        return output


class DiscoveryChatModel(ChatModelBase):
    """AgentScope model interface backed by bounded JSON/SSE transport."""

    def __init__(self, client, max_tool_calls: int):
        super().__init__(model_name=client.config.model,stream=client.config.stream)
        self.client=client
        self.max_tool_calls=max_tool_calls
        self.total_calls=0

    async def __call__(self, messages: list[dict], tools=None, tool_choice=None, **kwargs):
        from .agent import _message

        _check_runtime_policy()
        self.client.budget.check_deadline()
        try:
            parsed=self.client._request(messages,_message,tools,
                tool_choice='none' if self.total_calls>=self.max_tool_calls else 'auto')
        finally:
            # HTTP has finished or failed. Formatted prompts must not outlive it.
            messages.clear()
        if 'final' in parsed:
            content=[{'type':'text','text':parsed['final'].model_dump_json()}]
        else:
            calls=parsed['calls']
            if self.total_calls+len(calls)>self.max_tool_calls:
                raise BudgetExceeded('discovery_tool_calls_exhausted')
            content=[]
            for call in calls:
                self.total_calls+=1
                try:
                    args=json.loads(call['arguments'])
                except (ValueError,TypeError):
                    args=call['arguments']  # Toolkit rejects non-object inputs.
                content.append({'type':'tool_use','id':f'call_{self.total_calls}',
                                'name':call['name'],'input':args})
        response=ChatResponse(content=content)
        if self.stream:
            async def completed_stream():
                yield response
            return completed_stream()
        return response


class DiscoveryReActAgent(ReActAgent):
    """Use the framework loop with fail-closed limits and no raw output channel."""

    # Isolate from global hooks configured for unrelated agents in the host app.
    # Hooks receive whole Msg objects, including ephemeral source values.
    for _hook in ('reply','print','observe','reasoning','acting'):
        locals()[f'_class_pre_{_hook}_hooks']=OrderedDict()
        locals()[f'_class_post_{_hook}_hooks']=OrderedDict()
    del _hook

    async def print(self, msg, last=True, **kwargs) -> None:
        # Console, Studio and message queues must not receive raw samples.
        return None

    def state_dict(self) -> dict:
        raise LightGraphError('agent_source_persistence_forbidden')

    def load_state_dict(self, state_dict: dict, strict: bool=True) -> None:
        raise LightGraphError('agent_source_persistence_forbidden')

    async def _reasoning(self, tool_choice=None) -> Msg:
        if getattr(self,'_terminal_failure',None) is not None:
            raise self._terminal_failure
        _check_runtime_policy()
        return await super()._reasoning(tool_choice)

    async def _acting(self, tool_call):
        if getattr(self,'_terminal_failure',None) is not None:
            return None
        try:
            _check_runtime_policy()
            return await super()._acting(tool_call)
        except (LightGraphError,asyncio.CancelledError) as exc:
            # AgentScope pre-creates the batch's coroutines. Drain them without
            # executing further tools after budget/policy failure or cancellation,
            # then stop before the next model call.
            self._terminal_failure=exc
            return None

    async def _summarizing(self) -> Msg:
        if getattr(self,'_terminal_failure',None) is not None:
            raise self._terminal_failure
        # Framework's default summary would spend one extra LLM request.
        raise BudgetExceeded('discovery_steps_exhausted')


async def run_agent(client, packet, executor, dispatch, system, *, max_steps, max_tool_calls):
    from .agentscope_tools import create_toolkit

    _check_runtime_policy()
    memory=EphemeralMemory()
    toolkit=create_toolkit(executor,dispatch)
    model=DiscoveryChatModel(client,max_tool_calls)
    agent=DiscoveryReActAgent(name=client.config.agent_name,sys_prompt=system,
        model=model,formatter=DiscoveryFormatter(packet,executor),toolkit=toolkit,
        memory=memory,max_iters=max_steps,parallel_tool_calls=False,
        enable_meta_tool=False,print_hint_msg=False)
    agent.set_console_output_enabled(False)
    reply=None
    try:
        # The actual AgentScope loop, not the former hand-written while loop.
        reply=await agent.reply(Msg('user','discovery task','user'))
        return ProposalBatch.model_validate_json(reply.get_text_content())
    finally:
        # InMemoryMemory stores copies; clear originals as well as the returned
        # message, and remove closures retaining the executor or packet.
        for message in await memory.get_memory():
            message.content=[]
            message.metadata=None
        await memory.clear()
        memory._compressed_summary=''
        if reply is not None:
            reply.content=[]
        toolkit.tools.clear()
        toolkit._argument_validators.clear()
        agent._terminal_failure=None
        agent._sys_prompt=''
        agent.formatter.packet={}
        agent.formatter.executor=None
