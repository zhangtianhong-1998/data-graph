"""Bound ephemeral history without changing tool identities or inventing evidence."""
from lightgraph.contracts import canonical
from .llm import _request_schema, request_envelope_for, response_format_for
from .token_estimation import estimate_tokens


def prompt_bytes(messages, tools, mode):
    return prompt_size(messages, tools, mode)[0]


def prompt_size(messages, tools, mode):
    """Size the complete transport envelope, including tool schemas."""
    response_format=None if tools else response_format_for(_request_schema(messages),mode)
    content=canonical(request_envelope_for(messages,tools,response_format))
    return len(content.encode()), estimate_tokens(content)


def prompt_fits(messages, tools, mode, byte_limit, token_limit=None):
    byte_count,token_count=prompt_size(messages,tools,mode)
    return byte_count<=byte_limit and (token_limit is None or token_count<=token_limit)


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
