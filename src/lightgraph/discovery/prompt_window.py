"""Bound ephemeral history without changing tool identities or inventing evidence."""
from lightgraph.contracts import canonical
from .llm import _request_schema, request_envelope_for, response_format_for
from .token_estimation import estimate_tokens
from .source_digest import SOURCE_TOOLS, decoded_result, source_result_digest


def prompt_bytes(messages, tools, mode):
    return prompt_size(messages, tools, mode)[0]


def prompt_size(messages, tools, mode):
    """Size the complete transport envelope, including tool schemas."""
    response_format=None if tools else response_format_for(_request_schema(messages),mode)
    content=canonical(request_envelope_for(messages,tools,response_format))
    return len(content.encode()), estimate_tokens(content)


def prompt_fits(messages, tools, mode, byte_limit, token_limit=None):
    byte_count,token_count=prompt_size(messages,tools,mode)
    return (byte_limit is None or byte_count<=byte_limit) and (token_limit is None or token_count<=token_limit)


def _compactable_exchanges(messages, submission_names):
    """Only complete source rounds may leave the request, never feedback."""
    groups=[]
    index=0
    while index<len(messages):
        message=messages[index]
        calls=message.get('tool_calls') if message.get('role')=='assistant' else None
        if not isinstance(calls,list) or not calls:
            index+=1
            continue
        end=index+1
        while end<len(messages) and messages[end].get('role')=='tool':
            end+=1
        replies=messages[index+1:end]
        if all(isinstance(call,dict) and isinstance(call.get('function'),dict) for call in calls):
            ids=[call.get('id') for call in calls]
            names=[call['function'].get('name') for call in calls]
            reply_ids=[reply.get('tool_call_id') for reply in replies]
            error_feedback=any((decoded_result(reply.get('content')) or {}).get('status')=='error'
                               for reply in replies)
            if (all(isinstance(call_id,str) and call_id for call_id in ids)
                    and len(ids)==len(set(ids)) and len(replies)==len(ids)
                    and all(isinstance(call_id,str) for call_id in reply_ids)
                    and set(reply_ids)==set(ids)
                    and all(isinstance(name,str) and name in SOURCE_TOOLS
                            and name not in submission_names for name in names)
                    and not error_feedback):
                groups.append((index,end,dict(zip(ids,names))))
        index=end
    return groups


def compact_source_history(messages, tools, mode, submission_names, *, target_tokens=32768):
    """Fit an independent soft working window without changing the raw transcript.

    Older source results are first projected to references, column headers and
    compiler counts. If necessary, whole source rounds are then omitted. The
    newest complete source exchange and every submission/validation exchange
    stay verbatim. System and user context are not rewritten. The caller can
    expose the value-free metrics in runtime control and the execution trace.

    A target smaller than pinned material reports pressure, not a hard failure:
    provider input limits remain the responsibility of ``fit_source_history``.
    """
    if target_tokens is not None and (type(target_tokens) is not int or target_tokens<1):
        raise ValueError('invalid_working_context_tokens')
    before_bytes,before_tokens=prompt_size(messages,tools,mode)
    metrics={'before_bytes':before_bytes,'before_tokens':before_tokens,
        'after_bytes':before_bytes,'after_tokens':before_tokens,
        'target_tokens':target_tokens,'target_unmet':False,
        'compressed_results':0,'omitted_source_exchanges':0}
    fitted=list(messages)
    if target_tokens is None or before_tokens<=target_tokens:
        return fitted,metrics
    groups=_compactable_exchanges(messages,submission_names)
    eligible=groups[:-1]
    for start,end,names in eligible:
        for index in range(start+1,end):
            reply=messages[index]
            compacted=source_result_digest(reply.get('content'),names[reply['tool_call_id']])
            if compacted is not None:
                fitted[index]={**reply,'content':compacted}
                metrics['compressed_results']+=1
        metrics['after_bytes'],metrics['after_tokens']=prompt_size(fitted,tools,mode)
        if metrics['after_tokens']<=target_tokens:
            break
    omitted=set()
    projected=fitted
    for start,end,_ in eligible:
        if metrics['after_tokens']<=target_tokens:
            break
        omitted.update(range(start,end))
        metrics['omitted_source_exchanges']+=1
        fitted=[message for index,message in enumerate(projected) if index not in omitted]
        metrics['after_bytes'],metrics['after_tokens']=prompt_size(fitted,tools,mode)
    metrics['target_unmet']=metrics['after_tokens']>target_tokens
    return fitted,metrics


def fit_source_history(messages, tools, mode, limit, submission_names, *, token_limit=None):
    """Evict oldest complete source exchanges, keeping the newest and all drafts.

    Local submissions, declarations and validation feedback are pinned verbatim.
    No individual tool response is removed from a multi-call exchange. If even
    the remaining request cannot fit, the transport's ordinary guard rejects it.
    """
    groups=[]
    index=0
    while index<len(messages):
        message=messages[index]
        calls=message.get('tool_calls',[]) if message.get('role')=='assistant' else []
        if not calls:
            index+=1
            continue
        end=index+1
        while end<len(messages) and messages[end].get('role')=='tool':
            end+=1
        ids=[call.get('id') for call in calls]
        replies=[reply.get('tool_call_id') for reply in messages[index+1:end]]
        if (len(ids)==len(set(ids)) and len(replies)==len(ids) and set(replies)==set(ids)
                and all(call.get('function',{}).get('name') not in submission_names for call in calls)):
            groups.append((index,end))
        index=end
    omitted=set()
    fitted=list(messages)
    removed=0
    # The newest source exchange stays available even when followed by a local
    # validation error; repairing that draft may still depend on its evidence.
    for start,end in groups[:-1]:
        if prompt_fits(fitted,tools,mode,limit,token_limit):
            break
        omitted.update(range(start,end))
        removed+=1
        fitted=[message for i,message in enumerate(messages) if i not in omitted]
        for i,message in enumerate(fitted):
            if message.get('role')=='system':
                fitted[i]={**message,'content':(message.get('content') or '')+'\n'+canonical({
                    'history_window':{'omitted_source_exchanges':removed,
                        'note':'Older source exchanges are outside this request window. '
                        'Their absence is not negative evidence. The current task, schema knowledge, '
                        'issued-reference registry and draft feedback remain visible with unchanged '
                        'candidate/validated status; omission does not validate any claim. '
                        'If a missing result is needed, query the authorized source again within '
                        'the remaining allowance. Never reconstruct missing rows from memory; '
                        'finish with pending=true if evidence remains insufficient.'}})}
                break
    return fitted,removed
