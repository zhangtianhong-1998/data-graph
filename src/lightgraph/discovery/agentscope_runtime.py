"""AgentScope runtime adapters with optional local, credential-redacted run history.

ReActAgent owns the loop, Toolkit owns registered tool dispatch, and the native
OpenAI adapter owns protocol parsing. Only complete model responses reach the
framework, so an interrupted argument stream can never execute a query.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from uuid import uuid4

from agentscope import _config as framework_config
from agentscope.agent import ReActAgent
from agentscope.formatter import FormatterBase
from agentscope.memory import InMemoryMemory
from agentscope.message import Msg
from agentscope.model import ChatModelBase, ChatResponse

from lightgraph.context import (discovery_envelope, bounded_discovery_packet,
                                with_discovery_runtime_control)
from lightgraph.contracts import canonical
from lightgraph.errors import BudgetExceeded, LightGraphError
from lightgraph.runtime.trace import emit_trace, trace_context
from .models import ProposalBatch
from .proposal_session import (DiscoveryFinish, ProposalSession, proposal_tool_specs,
                               MAX_SUBMISSION_CALLS)


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

    def __init__(self, packet: dict, executor, references=None, submission_names=()):
        self.packet=packet
        self.executor=executor
        self.references=references
        self.submission_names=set(submission_names)

    @staticmethod
    def _deduplicate_tool_context(content: str, documents: list[dict]) -> str:
        """Reference exact visible copies without hiding new document evidence.

        Tool metadata may carry the same scoped preview on every result. Only
        those identical objects are reused from the current task packet; search
        pages, revisions and partial sections remain in their original fields.
        """
        try:
            result=json.loads(content)
        except (TypeError,ValueError):
            return content
        from .table_presentation import present_tool_result
        presented=present_tool_result(result)
        if presented is not result:
            result=presented
            content=canonical(result)
        if not isinstance(result,dict) or not isinstance(result.get('context'),dict):
            return content
        context=result['context']
        if not isinstance(context.get('documents'),list):
            return content
        visible={canonical(section):index for index,section in enumerate(documents)
                 if isinstance(section,dict)}
        retained=[]
        references=[]
        for section in context['documents']:
            index=visible.get(canonical(section)) if isinstance(section,dict) else None
            if index is None:
                retained.append(section)
            else:
                references.append(f'input.document_snippets[{index}]')
        if not references:
            return content
        context['documents']=retained
        context['documents_reused_from_input']=references
        return canonical(result)

    async def format(self, msgs: list[Msg]) -> list[dict]:
        _check_runtime_policy()
        if callable(getattr(self.executor,'context_documents',None)):
            self.packet['document_snippets']=self.executor.context_documents()
            self.packet['document_refs']=self.executor.document_refs()
            self.packet['context_tables']=self.executor.context_tables()
        visible=bounded_discovery_packet(self.packet)
        packet_text=canonical(discovery_envelope(visible))
        output=[]
        task_inserted=False
        for msg in msgs:
            if msg.role=='user' and not task_inserted:
                output.append({'role':'user','content':packet_text})
                task_inserted=True
            elif msg.has_content_blocks('tool_result'):
                for block in msg.get_content_blocks('tool_result'):
                    content=block['output']
                    if not isinstance(content,list) or any(b.get('type')!='text' for b in content):
                        raise LightGraphError('agent_nontext_tool_response')
                    output.append({'role':'tool','tool_call_id':block['id'],
                        'content':self._deduplicate_tool_context(
                            ''.join(b['text'] for b in content),visible['document_snippets'])})
            elif msg.has_content_blocks('tool_use'):
                calls=[]
                for block in msg.get_content_blocks('tool_use'):
                    args=block['input']
                    calls.append({'id':block['id'],'type':'function','function':{
                        'name':block['name'],'arguments':args if isinstance(args,str) else canonical(args)}})
                output.append({'role':'assistant','content':None,'tool_calls':calls})
            else:
                output.append({'role':msg.role,'content':msg.get_text_content() or ''})
        return self.references.messages(output,self.submission_names) if self.references else output


class DiscoveryChatModel(ChatModelBase):
    """Reserve submission and feedback within the configured discovery allowance.

    Providers may return multiple tools even when parallel calls are disabled.
    An oversized batch executes nothing; its remaining turn is used to submit
    the evidence already obtained, without increasing either allowance. A stale
    source call during submission receives one correction within that same limit.
    """

    def __init__(self, client, max_tool_calls: int, max_steps: int, submission_names=(), references=None):
        super().__init__(model_name=client.config.model,stream=client.config.stream)
        self.client=client
        self.max_tool_calls=max_tool_calls
        self.max_steps=max_steps
        self.total_calls=0
        self.source_calls=0
        self.submission_calls=0
        self.submission_names=set(submission_names)
        self.steps=0
        self.submission_reason=None
        self.phase_corrections=0
        self.references=references
        self.call_durations=[]
        self.timeout_submission_attempts=0

    def _submission_reserve(self):
        # Recent successful turn latency is a scheduling estimate, not a new
        # deadline or permission to exceed shared model/time allowances.
        return 2*max(self.call_durations[-3:],default=0.0)

    def _request_context(self, messages, reason):
        """Add runtime-owned counters, never tool arguments or source values."""
        from .context_projection import deduplicate_request_context
        # Initial request preflight has no running clock. Keep its exact wire
        # contract stable; attach timing only when it changes submission policy.
        timing=({'remaining_seconds':self.client.budget.remaining_seconds(),
                 'submission_reserve_seconds':self._submission_reserve()}
                if reason in {'time_submission_window','request_timeout_submission'} else {})
        prepared=with_discovery_runtime_control(messages,turn=self.steps+1,max_turns=self.max_steps,
            remaining_tool_calls=self.max_tool_calls-self.source_calls,submission_reason=reason,
            remaining_submission_calls=MAX_SUBMISSION_CALLS-self.submission_calls,
            **timing)
        projected,metrics=deduplicate_request_context(prepared)
        if metrics['projected_results']:
            for field,value in metrics.items():
                key='request_context_'+field
                self.client.usage[key]=self.client.usage.get(key,0)+value
            emit_trace('request_context_deduplicated',{'step':self.steps+1,**metrics})
        return projected

    def _reject_phase_batch(self, messages, available, reason, calls, *, can_retry):
        """Reject the whole batch; expose only runtime counters and issued names."""
        feedback={'code':'source_tool_unavailable_during_submission','executed_tools':0,
            'requested_tools':len(calls),'submission_reason':reason,
            'remaining_turns':self.max_steps-self.steps,
            'allowed_tools':[spec['function']['name'] for spec in available],
            'retry_available':can_retry,
            'next_action':'No tool in this batch executed, including submissions. '
                'Use the listed declaration/submission tools to submit retained findings and '
                'finish_discovery. Resubmit any local proposals from this rejected batch. '
                'If evidence is insufficient, finish with pending=true; never invent evidence.'}
        usage=self.client.usage
        usage['discovery_phase_rejections']=usage.get('discovery_phase_rejections',0)+1
        emit_trace('discovery_phase_feedback',{'step':self.steps,**feedback})
        if can_retry:
            messages.append({'role':'user','content':canonical({'runtime_feedback':feedback})})

    async def __call__(self, messages: list[dict], tools=None, tool_choice=None, **kwargs):
        from .agent import _message
        from .prompt_window import (prompt_bytes, prompt_size, prompt_fits,
                                    fit_source_history, compact_source_history)
        from .token_estimation import prompt_token_limit

        _check_runtime_policy()
        self.client.budget.check_deadline()
        if self.references:
            tools=self.references.schema(tools)
        def parse(message):
            return _message(self.references.response(message,self.submission_names)
                            if self.references else message)
        correcting_phase=False
        try:
            while True:
                if self.steps>=self.max_steps:
                    raise BudgetExceeded('discovery_steps_exhausted')
                reason=self.submission_reason
                if reason is None and self.source_calls>=self.max_tool_calls:
                    reason='tool_allowance_exhausted'
                if reason is None and self.steps==self.max_steps-1:
                    reason='last_turn'
                if reason is None and self.max_steps>=4 and self.steps==self.max_steps-2:
                    # Leave one response after a local submission so a rejected
                    # field can be corrected and acknowledged before finishing.
                    # Very short tasks retain their single final turn; no call
                    # is added beyond either the task or shared model budget.
                    reason='submission_window'
                if reason is None and tool_choice=='none':
                    reason='caller_requested'
                if (reason is None and self.submission_names and self.call_durations
                        and self.client.budget.remaining_seconds()<=self._submission_reserve()):
                    self.submission_reason=reason='time_submission_window'
                    self.client.usage['time_submission_windows']=self.client.usage.get('time_submission_windows',0)+1
                    emit_trace('discovery_time_submission',{'step':self.steps+1,
                        'remaining_seconds':self.client.budget.remaining_seconds(),
                        'submission_reserve_seconds':self._submission_reserve()})
                available=tools or []
                if reason:
                    available=[spec for spec in available
                        if spec['function']['name'] in self.submission_names]
                if self.submission_calls>=MAX_SUBMISSION_CALLS:
                    available=[spec for spec in available
                        if spec['function']['name'] not in self.submission_names]
                if tool_choice=='none':
                    available=[]
                request_messages=self._request_context(messages,reason)
                mode=self.client.config.response_format
                size,estimated_tokens=prompt_size(request_messages,available,mode)
                token_limit=prompt_token_limit(self.client.config)
                byte_limit=self.client.config.max_prompt_bytes
                working_target=self.client.config.working_context_tokens
                if working_target is not None:
                    request_messages,compaction=compact_source_history(request_messages,available,mode,
                        self.submission_names,target_tokens=min(working_target,token_limit)
                            if token_limit is not None else working_target)
                    if compaction['compressed_results'] or compaction['omitted_source_exchanges'] or compaction['target_unmet']:
                        usage=self.client.usage
                        usage['context_compaction_turns']=usage.get('context_compaction_turns',0)+1
                        for metric in ('compressed_results','omitted_source_exchanges'):
                            key='context_compaction_'+metric
                            usage[key]=usage.get(key,0)+compaction[metric]
                        if compaction['target_unmet']:
                            key='context_compaction_target_unmet'
                            usage[key]=usage.get(key,0)+1
                        for stage in ('before','after'):
                            key='context_compaction_peak_'+stage+'_estimated_tokens'
                            usage[key]=max(usage.get(key,0),compaction[stage+'_tokens'])
                        emit_trace('context_compaction',{'step':self.steps+1,**compaction})
                if not prompt_fits(request_messages,available,mode,byte_limit,token_limit) and any(m.get('role')=='tool' for m in messages):
                    # First trim old complete exchanges while retaining the
                    # ability to reread evidence within the discovery allowance.
                    request_messages,removed=fit_source_history(request_messages,available,mode,
                        byte_limit,self.submission_names,token_limit=token_limit)
                    if not prompt_fits(request_messages,available,mode,byte_limit,token_limit):
                        self.submission_reason=reason='context_window_pressure'
                        available=[spec for spec in available
                            if spec['function']['name'] in self.submission_names]
                        request_messages=self._request_context(messages,reason)
                        if working_target is not None:
                            request_messages,_=compact_source_history(request_messages,available,mode,
                                self.submission_names,target_tokens=min(working_target,token_limit)
                                    if token_limit is not None else working_target)
                        request_messages,removed=fit_source_history(request_messages,available,mode,
                            byte_limit,self.submission_names,token_limit=token_limit)
                    usage=self.client.usage
                    usage['prompt_pressure_turns']=usage.get('prompt_pressure_turns',0)+1
                    usage['prompt_history_exchanges_omitted']=usage.get('prompt_history_exchanges_omitted',0)+removed
                    usage['prompt_peak_before_window_bytes']=max(usage.get('prompt_peak_before_window_bytes',0),size)
                    usage['prompt_peak_after_window_bytes']=max(usage.get('prompt_peak_after_window_bytes',0),
                        prompt_bytes(request_messages,available,mode))
                    usage['prompt_peak_before_window_estimated_tokens']=max(
                        usage.get('prompt_peak_before_window_estimated_tokens',0),estimated_tokens)
                    usage['prompt_peak_after_window_estimated_tokens']=max(
                        usage.get('prompt_peak_after_window_estimated_tokens',0),
                        prompt_size(request_messages,available,mode)[1])
                    emit_trace('context_window',{'step':self.steps+1,'removed_exchanges':removed,
                        'before_estimated_tokens':estimated_tokens,
                        'after_estimated_tokens':prompt_size(request_messages,available,mode)[1],
                        'reason':reason,'token_limit':token_limit,'byte_limit':byte_limit})
                self.steps+=1
                request_started_at=time.monotonic()
                try:
                    # Source access ends at its configured allowance or final
                    # turn. Bounded local submissions can still preserve work.
                    # A correction uses only the currently offered tools. This
                    # consumes an ordinary turn and the shared model budget.
                    choice=('required' if correcting_phase else (tool_choice or 'auto')) if available else 'none'
                    with trace_context(discovery_step=self.steps,max_discovery_steps=self.max_steps,
                                       submission_reason=reason,phase_correction=correcting_phase):
                        parsed=self.client._request(request_messages,parse,available or None,
                            tool_choice=choice)
                    self.call_durations.append(max(0.0,time.monotonic()-request_started_at))
                except LightGraphError as exc:
                    # An unread response executed no tools. If the transport
                    # declined a hopeless same-payload retry, spend at most one
                    # remaining discovery turn switching to local submission.
                    # A timeout during submission itself gets no extra loop.
                    if (exc.code=='model_transport_error' and getattr(exc,'retry_suppressed',False)
                            and getattr(exc,'failure_kind',None)=='read_timeout'
                            and not reason and self.submission_names and self.steps<self.max_steps
                            and self.submission_calls<MAX_SUBMISSION_CALLS
                            and not self.timeout_submission_attempts):
                        self.client.budget.check_deadline()
                        self.timeout_submission_attempts+=1
                        self.submission_reason='request_timeout_submission'
                        key='timeout_submission_attempts'
                        self.client.usage[key]=self.client.usage.get(key,0)+1
                        emit_trace('discovery_timeout_submission',{'failed_step':self.steps,
                            'remaining_turns':self.max_steps-self.steps,
                            'remaining_seconds':self.client.budget.remaining_seconds(),
                            'reason':getattr(exc,'retry_skip_reason',None),'executed_tools':0})
                        messages.append({'role':'user','content':canonical({'runtime_feedback':{
                            'code':'request_timeout_submission','executed_tools':0,
                            'next_action':'Submit retained findings and finish. Missing evidence stays pending.'}})})
                        continue
                    raise
                finally:
                    request_messages.clear()
                if 'final' in parsed or 'finish' in parsed:
                    if correcting_phase:
                        usage=self.client.usage
                        usage['discovery_phase_recoveries']=usage.get('discovery_phase_recoveries',0)+1
                    break
                calls=parsed['calls']
                source_count=sum(call['name'] not in self.submission_names for call in calls)
                local_count=len(calls)-source_count
                if tool_choice=='none' or reason and source_count:
                    if reason and source_count and tool_choice!='none':
                        can_retry=(self.steps<self.max_steps and self.phase_corrections<1 and bool(available))
                        self._reject_phase_batch(messages,available,reason,calls,can_retry=can_retry)
                        if can_retry:
                            self.phase_corrections+=1
                            correcting_phase=True
                            parsed.clear()
                            continue
                    if self.steps>=self.max_steps:
                        raise BudgetExceeded('discovery_steps_exhausted')
                    raise LightGraphError('discovery_final_response_required')
                if self.submission_calls+local_count>MAX_SUBMISSION_CALLS:
                    raise BudgetExceeded('discovery_tool_calls_exhausted')
                if self.source_calls+source_count<=self.max_tool_calls:
                    if correcting_phase:
                        usage=self.client.usage
                        usage['discovery_phase_recoveries']=usage.get('discovery_phase_recoveries',0)+1
                    break
                # The last turn was reserved before the request. Reject the
                # entire oversized batch instead of executing a changed subset.
                self.submission_reason='tool_batch_rejected'
                messages.append({'role':'user','content':canonical({'runtime_feedback':{
                    'code':'tool_batch_exceeds_remaining_allowance','executed_tools':0,
                    'requested_tools':len(calls),
                    'remaining_tool_calls':self.max_tool_calls-self.source_calls,
                    'next_action':'Finish retained drafts or abstain using the small final response.'}})})
                parsed.clear()
        finally:
            # HTTP has finished or failed. Formatted prompts must not outlive it.
            messages.clear()
        if 'final' in parsed or 'finish' in parsed:
            content=[{'type':'text','text':parsed.get('final',parsed.get('finish')).model_dump_json()}]
        else:
            calls=parsed['calls']
            content=[]
            for call in calls:
                self.total_calls+=1
                if call['name'] in self.submission_names:self.submission_calls+=1
                else:self.source_calls+=1
                try:
                    from .llm import _strict_json_loads
                    args=_strict_json_loads(call['arguments'])
                except (ValueError,TypeError):
                    args=call['arguments']  # Toolkit rejects non-object inputs.
                content.append({'type':'tool_use','id':call['id'],
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
        if getattr(self,'_proposal_session',None) is not None and self._proposal_session.finished:
            return Msg(self.name,DiscoveryFinish(action='finish_discovery').model_dump_json(),'assistant')
        return await super()._reasoning(tool_choice)

    async def _acting(self, tool_call):
        if getattr(self,'_terminal_failure',None) is not None:
            return None
        if getattr(self,'_proposal_session',None) is not None and self._proposal_session.finished:
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
        if getattr(self,'_proposal_session',None) is not None and self._proposal_session.finished:
            return Msg(self.name,DiscoveryFinish(action='finish_discovery').model_dump_json(),'assistant')
        # The model reserves its final configured turn for submission. Never
        # add the framework's unbudgeted summary if the provider ignores it.
        raise BudgetExceeded('discovery_steps_exhausted')


async def run_agent(client, packet, executor, dispatch, system, *, max_steps, max_tool_calls):
    from .agentscope_tools import create_toolkit
    from .model_references import ModelReferences

    _check_runtime_policy()
    memory=EphemeralMemory()
    session=ProposalSession(packet)
    references=ModelReferences(packet)
    class CombinedTools:
        def tool_specs(self):
            return [*executor.tool_specs(),*proposal_tool_specs(packet['task'], packet)]

    def combined_dispatch(name,arguments):
        if name in session.names:
            client.budget.check_deadline()
            return session.execute(name,arguments)
        return dispatch(name,arguments)

    toolkit=create_toolkit(CombinedTools(),combined_dispatch)
    toolkit.invalid_argument_handler=lambda name,args,diagnostics: (
        session.reject_tool_arguments(name,args,diagnostics) if name in session.names else None)
    model=DiscoveryChatModel(client,max_tool_calls,max_steps,session.names,references)
    agent=DiscoveryReActAgent(name=client.config.agent_name,sys_prompt=system,
        model=model,formatter=DiscoveryFormatter(packet,executor,references,session.names),toolkit=toolkit,
        memory=memory,max_iters=max_steps,parallel_tool_calls=False,
        enable_meta_tool=False,print_hint_msg=False)
    agent.set_console_output_enabled(False)
    agent._proposal_session=session
    reply=None
    termination_code=None
    trace_binding=trace_context(agent_id='agent_'+uuid4().hex,task=packet.get('task'))
    trace_binding.__enter__()
    emit_trace('agent_start',{'packet':packet,'system':system,
        'max_steps':max_steps,'max_tool_calls':max_tool_calls})
    try:
        # The actual AgentScope loop, not the former hand-written while loop.
        try:
            reply=await agent.reply(Msg('user','discovery task','user'))
        except LightGraphError as exc:
            termination_code=exc.code
            # Recover only transport/shape/allowance failures. Authorization,
            # tracing policy changes and cancellation still terminate the run.
            recoverable=(exc.code.startswith(('llm_','model_transport_','model_json_','model_stream_')) or exc.code in {
                'model_packet_too_large','model_response_too_large','discovery_steps_exhausted',
                'discovery_tool_calls_exhausted','discovery_final_response_required',
                'model_schema_invalid','model_response_invalid','model_response_truncated',
                'model_stream_invalid','model_http_error','model_rate_limited',
                'wall_time_budget_exhausted','discovery_time_budget_exhausted',
                'run_time_budget_exhausted'})
            if not (session.drafts or session.invalid_count or session.object_models) or not recoverable:
                raise
            batch=session.batch(pending_code='discovery_submission_incomplete')
            if exc.code not in batch._runtime_pending_codes:
                batch._runtime_pending_codes.append(exc.code)
            return batch
        if session.finished:
            return session.batch()
        raw=json.loads(reply.get_text_content())
        if isinstance(raw,dict) and 'action' in raw:
            finish=DiscoveryFinish.model_validate(raw)
            if finish.action!='abstain':
                result=session.execute('finish_discovery',finish.model_dump(exclude={'action'}))
                if result.get('status')!='ok':
                    return session.batch(pending_code='discovery_submission_incomplete')
            else:
                # Withdrawing drafts does not cancel an explicit declaration
                # that relevant work remains unfinished.
                session.pending=finish.pending
            return session.batch(abstain=finish.action=='abstain')
        legacy=ProposalBatch.model_validate(raw)
        if legacy.abstain:
            # Legacy global abstention is also a withdrawal, never a request
            # to finish and publish the previously staged candidates.
            return session.batch(abstain=True)
        if not session.drafts and session.schema.is_empty() and not session.invalid_count and not session.object_models:
            from .object_mapping_feedback import object_mapping_diagnostics
            if not any(object_mapping_diagnostics(proposal,packet) for proposal in legacy.proposals):
                legacy._runtime_submission_complete=True
                return legacy
        # A legacy final cannot replace staged exclusions or silently drop an
        # earlier valid hypothesis. Union all locally valid drafts instead.
        invalid_before=session.invalid_count
        if legacy.task!=session.task:
            session.reject('model_task_mismatch')
        else:
            session.execute('declare_semantic_schema',legacy.semantic_schema.model_dump(mode='json'))
            for proposal in legacy.proposals:
                session.execute('submit_'+proposal.kind,proposal.model_dump(mode='json',exclude={'kind'}))
            session.pending=legacy.abstain
        if session.invalid_count>invalid_before:
            session.pending=True
        session.finished=True
        return session.batch()
    except BaseException as exc:
        termination_code=exc.code if isinstance(exc,LightGraphError) else type(exc).__name__
        raise
    finally:
        emit_trace('agent_end',{'pending':session.pending,'finished':session.finished,
            'drafts':session.drafts,'object_models':session.object_models,
            'invalid_count':session.invalid_count,'steps':model.steps,
            'max_steps':model.max_steps,'source_calls':model.source_calls,
            'submission_calls':model.submission_calls,'phase_corrections':model.phase_corrections,
            'termination_code':termination_code,
            'reply':reply.content if reply is not None else None})
        trace_binding.__exit__(None,None,None)
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
        toolkit.invalid_argument_handler=None
        session.clear()
        agent._proposal_session=None
        agent._terminal_failure=None
        agent._sys_prompt=''
        agent.formatter.packet={}
        agent.formatter.executor=None
        references.clear()
