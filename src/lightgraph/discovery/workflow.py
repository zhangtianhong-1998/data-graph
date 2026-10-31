"""Agent discovery produces rules; complete rule execution alone may publish them.

Only schema references, roles, counts and fixed issue codes escape this module.
The Agent and query tools own ephemeral observations and never persist transcripts.
"""
from __future__ import annotations

from pydantic import ValidationError

from lightgraph.contracts import (ExtractionGoal, ExcludedColumn, RuleSpec, IdentitySpec,
    ScopeSpec, RelationSpec, HierarchySpec, Dependencies, Coverage, digest)
from lightgraph.documents import DocumentCorpus
from lightgraph.errors import LightGraphError, BudgetExceeded
from .budget import BudgetAllocator, Cost
from .goal import source_in_goal, column_is_excluded, column_is_evidence_only
from .models import DiscoveryResult, Hypothesis, ProposalBatch, MAX_HIERARCHY_IDENTITY_REQUESTS
from .semantics import (declared_evidence, review_semantics, rule_evidence,
    hierarchy_identity_context, entity_granularity_identity, entity_granularity_context,
    entity_granularity_context_signatures, review_entity_granularity)
from .observations import ReviewObservations
from .semantic_revision import SemanticReviewFailure, candidate_record, revise_once
from lightgraph.semantic_schema import SemanticSchema, EntityTypeDefinition
from lightgraph.rules.endpoints import endpoint_columns
from .repair import (REPAIRABLE_PROPOSAL_CODES, SEMANTIC_REVIEW_PROTOCOL_CODES,
    PROPOSAL_REPAIR_GUIDANCE, MAX_PROPOSAL_REPAIR_ROUNDS, compiled_contract_error,
    HIERARCHY_IDENTITY_REPAIR_CODES, CONTRACT_CANDIDATE_CODES)


def _catalog(adapter, goal):
    from .engine import SENSITIVE
    result=[]
    for table in adapter.describe():
        if not source_in_goal(goal, table): continue
        copy=table.model_copy(deep=True)
        copy.columns=[c for c in copy.columns if not column_is_excluded(goal, table, c.name)
                      and not SENSITIVE.search(c.name)]
        names=set(copy.column_names)
        copy.unique_keys=[key for key in copy.unique_keys if set(key)<=names]
        copy.foreign_keys=[key for key in copy.foreign_keys if set(key.columns)<=names]
        result.append(copy)
    targets={t.qualified_name:set(t.column_names) for t in result}
    for table in result:
        table.foreign_keys=[fk for fk in table.foreign_keys
            if set(fk.target_columns)<=targets.get(f'{fk.target_schema}.{fk.target_table}',set())]
    return result


def _ask(model, packet, tools, observations):
    return model.explore(packet, tools,
        max_steps=getattr(model.config,'max_discovery_steps',8),
        max_tool_calls=getattr(model.config,'max_tool_calls',16),
        review_observations=observations)


def _exploration_order(tables):
    """Declared FK targets first; narrow tables first when dependencies allow.

    Scheduling is a cost heuristic, never evidence that a table is a definition
    or that matching codes share an identity. Cycles retain a deterministic exit.
    """
    keyed={(t.source_id,t.qualified_name):t for t in tables}
    positions={key:i for i,key in enumerate(keyed)}
    dependencies={key:{(table.source_id,f'{fk.target_schema}.{fk.target_table}')
        for fk in table.foreign_keys if (table.source_id,f'{fk.target_schema}.{fk.target_table}') in keyed
        and (table.source_id,f'{fk.target_schema}.{fk.target_table}')!=key}
        for key,table in keyed.items()}
    remaining=set(keyed);ordered=[]
    while remaining:
        ready=[key for key in remaining if not dependencies[key]&remaining]
        key=min(ready or remaining,key=lambda k:(len(keyed[k].columns),positions[k]))
        remaining.remove(key);ordered.append(keyed[key])
    return ordered


def _iteration_codes(context, table):
    context=context or {};codes=set(context.get('codes',[]))
    for item in context.get('work_items',[]):
        source=item.get('source',{})
        if (source.get('source_id'),source.get('schema',source.get('schema_name','public')),source.get('table')) == (
            table.source_id,table.schema_name,table.name):
            codes.update(item.get('pattern_codes',[]))
    return sorted(codes)


def _issue(result, table, code, *, count=None):
    entry={'table':table.qualified_name,'code':code,
           'status':'deferred' if (code in REPAIRABLE_PROPOSAL_CODES | SEMANTIC_REVIEW_PROTOCOL_CODES
               or any(token in code for token in ('budget','limit','exhausted','too_large'))) else 'unresolved'}
    if count is not None: entry['count']=count
    result.issues.append(entry)


def _record_submission_state(result, table, batch):
    """Keep valid drafts while reporting an unfinished Agent submission truthfully."""
    allowed = {'discovery_submission_incomplete', 'discovery_submission_pending',
               'discovery_invalid_drafts','discovery_steps_exhausted',
               'discovery_tool_calls_exhausted','discovery_final_response_required',
               'model_schema_invalid','model_response_invalid','model_response_truncated',
               'model_stream_invalid','model_http_error','model_rate_limited',
               'object_model_mapping_conflict'}
    pending = set(getattr(batch, '_runtime_pending_codes', ())) & allowed
    invalid = getattr(batch, '_runtime_invalid_drafts', 0)
    invalid = invalid if type(invalid) is int and invalid >= 0 else 0
    if invalid:
        result.stats['discovery_invalid_drafts'] = result.stats.get('discovery_invalid_drafts', 0) + invalid
        # Only static schema paths and categories survive; permitted evidence
        # choices and rejected input text remain in the transient tool feedback.
        for rejection,count in getattr(batch,'_runtime_invalid_codes',{}).items():
            result.issues.append({'table':table.qualified_name,'code':'discovery_draft_rejected',
                'reason_code':rejection,'count':count,'status':'diagnostic'})
        from lightgraph.contracts import canonical
        for detail in getattr(batch,'_runtime_invalid_diagnostics',[])[:64]:
            result.issues.append({'table':table.qualified_name,'code':'discovery_draft_field_diagnostic',
                'reason_code':detail['code'],'field_path':canonical(detail['path']),
                'constraint':detail['constraint'],'count':detail['count'],'status':'diagnostic'})
    for code in sorted(pending):
        result.issues.append({'table': table.qualified_name, 'code': code, 'status': 'deferred'})
    if pending:
        result.stats['discovery_pending_submissions'] = result.stats.get('discovery_pending_submissions', 0) + 1


def _preflight(rule, goal):
    """Check the same output contract as execution without registering anything."""
    from lightgraph.store.policy import PersistencePolicy, KINDS
    kind=rule.identity.entity_kind
    schema=rule.semantic_schema
    if (kind in goal.entity_kinds and kind not in KINDS['business']
        and kind not in {definition.name for definition in schema.entity_types}):
        schema=SemanticSchema.merged(schema,SemanticSchema(entity_types=[
            EntityTypeDefinition(name=kind,description=f'User-declared output type: {kind}')]))
    policy=PersistencePolicy().with_schema(schema)
    policy.check_rule(rule,goal)
    # Compatible built-in declarations are references; persist the normalized
    # project vocabulary rather than the model's redundant restatement.
    rule.semantic_schema=policy.semantic_schema


def _reference_schema(schema,source_kind,target_kind,relation,goal):
    """Reject impossible endpoint contracts before SQL or semantic review."""
    from lightgraph.store.policy import PersistencePolicy, KINDS
    kinds={source_kind,target_kind}
    defined={entry.name for entry in schema.entity_types}
    explicit=kinds & set(goal.entity_kinds) - KINDS['business'] - defined
    schema=SemanticSchema.merged(schema,SemanticSchema(entity_types=[EntityTypeDefinition(
        name=kind,description=f'User-declared output type: {kind}') for kind in sorted(explicit)]))
    schema=schema.for_usage(entity_kinds=kinds,relations={relation})
    policy=PersistencePolicy().with_schema(schema)
    endpoints=policy.relation_endpoint_kinds(relation)
    if not endpoints:raise LightGraphError('unregistered_relation')
    if source_kind not in endpoints[0] or target_kind not in endpoints[1]:
        raise LightGraphError('relation_endpoint_kind_mismatch')
    return policy.semantic_schema


def _reference_relation_contract(schema, relation):
    """Bind the executable relation contract into the exact review proposition."""
    from lightgraph.store.policy import PersistencePolicy
    policy=PersistencePolicy().with_schema(schema)
    definition=policy.relation_definitions.get(relation)
    if definition is not None:return definition.model_dump(mode='json')
    source_kinds,target_kinds=policy.relation_endpoint_kinds(relation)
    return {'name':relation,'source_kinds':sorted(source_kinds),'target_kinds':sorted(target_kinds),
        'allow_self_links':False,'definition_origin':'builtin'}


def _proposal_guards(batch, table, goal):
    from .engine import _column_id
    lookup={_column_id(table,c.name):c.name for c in table.columns}
    projection=set();identity=set()
    for proposal in batch.proposals:
        if proposal.kind=='exclusion':
            for item in proposal.claim.exclusions:
                if 'projection' in item.affects:projection.add(lookup[item.column_id])
                if 'identity' in item.affects:identity.add(lookup[item.column_id])
        elif proposal.kind=='column_group':
            for member in proposal.claim.members:
                if (member.exclude_from_projection or member.role in ('display_order','audit')
                    or member.role=='fact_value' and not goal.observation_values):
                    projection.add(lookup[member.column_id])
    return projection,identity


def _identity_columns(rule):
    columns=set(rule.identity.key_columns) | set(rule.scope.namespaces)
    if rule.hierarchy:
        columns.update(level.code for level in rule.hierarchy.levels)
        if rule.hierarchy.parent_column:columns.add(rule.hierarchy.parent_column)
    return columns


def _hierarchy_request_matches(proposal, request):
    """A renamed or narrowed overlapping identity still sees its old request."""
    previous=request['proposal']
    return (proposal.kind=='wide_hierarchy' and previous['kind']=='wide_hierarchy'
        and proposal.claim.entity_kind_hint==previous['claim']['entity_kind_hint']
        and bool({level.key for level in proposal.claim.levels}
                 & {level['key'] for level in previous['claim']['levels']}))


def _proposal_repair_record(proposal, code):
    clean=proposal.model_copy(update={'rationale':'','requested_probes':[]})
    return {'code':code,'guidance':PROPOSAL_REPAIR_GUIDANCE[code],
            'proposal':clean.model_dump(mode='json')}


def _compile(batch, table, goal, docs, result, *, model=None,packet=None,observations=None,failures=None,
             projection_excluded=(),identity_excluded=(),conflicted_ids=None,
             hierarchy_rules=(),granularity_rejections=None,hierarchy_context=(),
             compiled_proposals=None,identity_requests=None,policy_reviews=None,semantic_failures=None,
             conflict_records=None):
    from .engine import _compile_model, _check_hierarchy_identity_resolution, SENSITIVE
    from lightgraph.contracts import canonical
    # Inspect the whole batch before reviewing any sibling. Otherwise a renamed
    # default-policy sibling could be accepted before its explicit conflict.
    requests={canonical(item):item for item in [*(identity_requests or []),
        *(packet or {}).get('hierarchy_identity_requests',[])]}
    for proposal in batch.proposals:
        if proposal.kind!='wide_hierarchy':continue
        try:_check_hierarchy_identity_resolution(proposal.claim)
        except LightGraphError as exc:
            item=_proposal_repair_record(proposal,exc.code)
            requests.setdefault(canonical(item),item)
    if identity_requests is not None:identity_requests[:]=requests.values()
    excluded=[ExcludedColumn(column=c.name,reason='sensitive_or_excluded')
        for c in table.columns if SENSITIVE.search(c.name) or column_is_excluded(goal,table,c.name)]
    excluded.extend(ExcludedColumn(column=name,reason='proposal_exclusion') for name in sorted(projection_excluded))
    # Independent proposals should survive a rejected sibling, but every proposal
    # sees the same exclusions. An exclusion cannot be bypassed by another group.
    exclusions=[p for p in batch.proposals if p.kind=='exclusion']
    for p in batch.proposals:
        if p.kind=='column_group':
            from .engine import _column_id
            lookup={_column_id(table,c.name):c.name for c in table.columns}
            excluded.extend(ExcludedColumn(column=lookup[m.column_id],reason=m.role)
                for m in p.claim.members if m.exclude_from_projection or m.role in ('display_order','audit')
                or (m.role=='fact_value' and not goal.observation_values))
    hierarchies=list({canonical(context):context for context in [*hierarchy_context,
        *hierarchy_identity_context(batch,table,hierarchy_rules)]}.values())
    granularity_rejections={} if granularity_rejections is None else granularity_rejections
    rules=[];variants={}
    for p in batch.proposals:
        if p.kind=='exclusion': continue
        try:
            if goal.entity_kinds and getattr(p.claim,'entity_kind_hint',None) not in goal.entity_kinds:
                raise LightGraphError('outside_extraction_goal')
            schema=SemanticSchema.merged(goal.semantic_schema,batch.semantic_schema)
            local=ProposalBatch(task=batch.task,proposals=exclusions+[p],semantic_schema=schema)
            related_requests=[item for item in requests.values() if _hierarchy_request_matches(p,item)]
            review_packet=packet or {}
            from .conflicts import proposal_logical_id,guard_shape
            conflict_context=[item for item in review_packet.get('iteration_context',{}).get('rule_conflict_requests',[])
                if proposal_logical_id(p,table) in item.get('candidate',{}).get('rule_refs',[])]
            if review_packet.get('iteration_context',{}).get('rule_conflict_requests'):
                review_packet={**review_packet,'iteration_context':{
                    **review_packet['iteration_context'],'rule_conflict_requests':conflict_context,
                    'pending_candidates':[]}}
            if p.kind=='wide_hierarchy':
                if related_requests and len(requests)>MAX_HIERARCHY_IDENTITY_REQUESTS:
                    raise LightGraphError('hierarchy_identity_repair_context_limit')
                _check_hierarchy_identity_resolution(p.claim,require_explicit=bool(
                    related_requests or not requests and HIERARCHY_IDENTITY_REPAIR_CODES
                    & set((packet or {}).get('allowed_revision_codes',[]))))
                review_packet={**review_packet,'hierarchy_identity_requests':related_requests}
            if p.kind=='wide_hierarchy' and p.claim.path_semantics=='relative_path':
                # Missing executable semantics are a repairable proposal
                # contract, not a reason to pay for an incomplete review.
                if not p.claim.identity_domain or not p.claim.taxonomy or p.claim.position_semantics is None:
                    raise LightGraphError('hierarchy_position_semantics_missing')
                # The exploratory proposal permits arbitrary context keys;
                # check the executable contract before paying for a review.
                HierarchySpec(context_qualifiers=p.claim.context_qualifiers)
            review=None
            granularity_review=None
            # A rejected identity cannot become eligible merely because its
            # competing hierarchy failed compilation or vanished from a repair.
            grain_key=entity_granularity_identity(p)
            if grain_key is not None and grain_key in granularity_rejections:
                raise LightGraphError(granularity_rejections[grain_key])
            grain_context=entity_granularity_context(p,hierarchies)
            if not grain_context and (packet or {}).get('semantic_revision_guard') and p.kind in ('column_group','entity_key'):
                # Narrowing the key cannot make an obligatory review disappear.
                grain_context={'identity_columns':sorted(entity_granularity_identity(p)[-1])
                    if isinstance(entity_granularity_identity(p),tuple) else [], 'hierarchies':hierarchies}
            if grain_context:
                # A negative grain judgment remains terminal across sibling
                # repairs; changing a group ID or attribute cannot solicit a
                # different vote for the same entity identity.
                try:
                    granularity_review=review_entity_granularity(model,
                        {**(packet or {}),'goal':goal.model_dump(mode='json'),
                         'semantic_schema':schema.model_dump(mode='json')},
                        p,grain_context,docs,observations=observations)
                except LightGraphError as exc:
                    if exc.code in ('semantic_claim_refuted','semantic_claim_unresolved',
                                    'semantic_condition_unresolved'):
                        granularity_rejections[grain_key]=exc.code
                    raise
            sharing_requested=(p.kind in {'column_group','entity_key'}
                and p.claim.identity_resolution=='shared_definition')
            domain_requested=(p.kind in {'column_group','entity_key'}
                and p.claim.identity_resolution=='verified_identity_domain')
            scope_revision_review='namespace_scope_missing' in (packet or {}).get('allowed_revision_codes',[])
            if model is not None and (p.kind in ('long_hierarchy','wide_hierarchy','concept_mapping')
                                     or sharing_requested or domain_requested or scope_revision_review
                                     or conflict_context):
                review=review_semantics(model,{**review_packet,'semantic_schema':schema.model_dump(mode='json')},{
                    'kind':p.kind,'scope':(packet or {}).get('scope'),
                    'claim':p.claim.model_dump(mode='json')},docs,observations=observations)
            candidates=_compile_model(local,table,goal,docs,excluded,semantic_evidence=review)
            if not candidates:
                raise LightGraphError('entity_identity_missing')
            for rule in candidates:
                if _identity_columns(rule) & set(identity_excluded):
                    raise LightGraphError('excluded_column_in_identity')
                if rule.hierarchy and goal.relation_kinds and 'parent_of' not in goal.relation_kinds:
                    raise LightGraphError('outside_relation_goal')
                # An explicit declaration is optional. The Agent may infer
                # sharing, but its separate review must cover this exact claim.
                definition=rule.attributes.single_valued.get('definition')
                sharing=(review if sharing_requested else None) or declared_evidence(
                    docs,table,'shared_definition',[definition] if definition else [],
                    parameters={'namespace_columns':rule.scope.namespaces})
                if rule.kind=='column_group_entity' and not domain_requested and sharing and rule.scope.namespaces and rule.attributes.single_valued.get('definition'):
                    rule.identity.semantic_resolution='verified_definition_registry'
                    # The goal defines the inferred registry's namespace; a
                    # declaration continues to use its immutable document ID.
                    registry=('goal:'+goal.goal_id if sharing_requested else sharing[0].split('@',1)[0])
                    rule.identity.semantic_namespace='declared_definition:'+registry+':'+rule.identity.entity_kind
                    rule.evidence=rule_evidence(sharing,reviewed=bool(review),documents=docs)
                elif sharing_requested:
                    raise LightGraphError('shared_definition_attributes_missing')
                if (scope_revision_review or conflict_context) and review:
                    rule.evidence=rule_evidence(review,reviewed=True,documents=docs,
                                               probe_refs=rule.evidence.probe_refs)
                if granularity_review:
                    previous=(rule.evidence.semantic_basis if rule.evidence.semantic_status=='supported' else [])
                    rule.evidence=rule_evidence(sorted(set(previous+granularity_review)),
                        reviewed=True,documents=docs,probe_refs=rule.evidence.probe_refs)
                _preflight(rule,goal)
                for item in conflict_context:
                    if item.get('restored') and digest(guard_shape(rule)) not in {
                            m['guard_fingerprint'] for m in item['candidate']['mappings']}:
                        raise LightGraphError('rule_conflict_constraints_changed')
                rules.append(rule)
                variants.setdefault(rule.rule_id,[]).append((p.model_copy(deep=True),schema.model_copy(deep=True),rule))
                if compiled_proposals is not None:
                    compiled_proposals[rule.rule_id]=(p.model_copy(deep=True),schema.model_copy(deep=True),
                        entity_granularity_context_signatures(grain_context) if granularity_review else set())
                if policy_reviews is not None and p.kind=='wide_hierarchy' and review:
                    policy_reviews[rule.rule_id]={canonical(item) for item in related_requests}
        except (LightGraphError,ValidationError) as exc:
            code=(exc.code if isinstance(exc,LightGraphError) else compiled_contract_error(exc))
            if code is None:
                raise
            if isinstance(exc, SemanticReviewFailure):
                candidate = candidate_record(p, table, packet or {}, docs, exc)
                result.pending_candidates.append(candidate)
                if semantic_failures is not None:
                    semantic_failures.append({'candidate':candidate,'proposal':p,'failure':exc})
            elif code in (CONTRACT_CANDIDATE_CODES | SEMANTIC_REVIEW_PROTOCOL_CODES) and p.kind!='exclusion':
                from types import SimpleNamespace
                from .engine import _definition_document
                refs=[d['doc_id']+'@'+d['revision']+':'+d['section_id'] for d in docs
                      if _definition_document(d,table)]
                failure=SimpleNamespace(code=code,blocking=[],required=refs,
                    stage='concept_mapping' if p.kind=='concept_mapping' else 'entity_definition')
                result.pending_candidates.append(candidate_record(p,table,packet or {},docs,failure))
            if failures is not None and code in REPAIRABLE_PROPOSAL_CODES:
                failures.append((p,code))
            else:_issue(result,table,code)
    from .conflicts import merge_compatible_rules,conflict_record,mark_conflict_repaired
    unique={};conflicts=set()
    for rid,items in variants.items():
        merged=merge_compatible_rules([item[2] for item in items])
        if merged is not None:
            _preflight(merged,goal);unique[rid]=merged
            for item in (packet or {}).get('iteration_context',{}).get('rule_conflict_requests',[]):
                if item.get('restored') and rid in item['candidate']['rule_refs']:
                    mark_conflict_repaired(result,item['candidate']['candidate_id'],rid)
            if len(items)>1:result.stats['compatible_rule_merges']=result.stats.get('compatible_rule_merges',0)+1
            continue
        conflicts.add(rid);_issue(result,table,'conflicting_rule_proposals')
        result.issues[-1]['rule_id']=rid
        record=conflict_record(items,table,goal,packet or {},docs)
        result.pending_candidates.append(record['candidate'])
        if conflict_records is not None:conflict_records.append(record)
    if conflicted_ids is not None:conflicted_ids.update(conflicts)
    return [rule for rid,rule in unique.items() if rid not in conflicts]


def _compile_candidates(batch,table,goal,docs,result,*,model,packet,observations,
                        tool_factory=None,projection_excluded=(),identity_excluded=(),
                        hierarchy_rules=(),granularity_rejections=None,observed_hierarchies=None,
                        on_revision=None):
    """Preserve valid siblings and allow one structurally changed semantic alternative.

    Semantic judgments and full data validation remain separate. Actual data
    counterexamples use the scheduler's existing revision path; formatting or
    vocabulary errors stay explicit pending work rather than spawning repairs.
    """
    from lightgraph.contracts import canonical
    projection,identity=_proposal_guards(batch,table,goal)
    projection_excluded=set(projection_excluded)|projection
    identity_excluded=set(identity_excluded)|identity
    from .conflicts import retain_unchanged_conflicts
    batch=retain_unchanged_conflicts(batch,packet,docs,result,table)
    observed_hierarchies={} if observed_hierarchies is None else observed_hierarchies
    observed_hierarchies.update((canonical(context),context) for context in
        hierarchy_identity_context(batch,table,hierarchy_rules))
    failures=[]
    requests=list(packet.get('hierarchy_identity_requests',[]))
    policy_reviews={}
    compiled_proposals={}
    semantic_failures=[]
    conflict_records=[]
    granularity_rejections={} if granularity_rejections is None else granularity_rejections
    rules=_compile(batch,table,goal,docs,result,model=model,packet=packet,
        observations=observations,failures=failures,
        projection_excluded=projection_excluded,identity_excluded=identity_excluded,
        hierarchy_rules=hierarchy_rules,hierarchy_context=list(observed_hierarchies.values()),
        granularity_rejections=granularity_rejections,identity_requests=requests,
        policy_reviews=policy_reviews,compiled_proposals=compiled_proposals,semantic_failures=semantic_failures,
        conflict_records=conflict_records)
    from .conflicts import propose_conflict_revisions,guard_shape
    def ask_revision(model,repair_packet,tools,observations):
        revised=_ask(model,repair_packet,tools,observations)
        _record_submission_state(result,table,revised)
        if on_revision is not None:on_revision(revised,repair_packet)
        return revised
    conflict_alternatives,docs=propose_conflict_revisions(conflict_records,model=model,packet=packet,
        tool_factory=tool_factory,ask=ask_revision,observations=observations,result=result,table=table,documents=docs)
    for record,proposal,schema,repair_packet in conflict_alternatives:
        repaired=ProposalBatch(task='T5_revision',proposals=[proposal],
            semantic_schema=SemanticSchema.merged(batch.semantic_schema,schema))
        replacements=_compile(repaired,table,goal,docs,result,model=model,packet=repair_packet,
            observations=observations,projection_excluded=projection_excluded,identity_excluded=identity_excluded,
            hierarchy_rules=hierarchy_rules,hierarchy_context=list(observed_hierarchies.values()),
            granularity_rejections=granularity_rejections,identity_requests=requests,
            policy_reviews=policy_reviews,compiled_proposals=compiled_proposals)
        original_rules=[v[2] for v in record['variants']]
        accepted=[r for r in replacements if r.rule_id==original_rules[0].rule_id
            and any(guard_shape(r)==guard_shape(old) for old in original_rules)]
        if accepted:
            rules.extend(accepted);cid=record['candidate'].candidate_id
            from .conflicts import mark_conflict_repaired
            mark_conflict_repaired(result,cid,accepted[0].rule_id)
        elif replacements:_issue(result,table,'rule_conflict_constraints_changed')
    alternatives,docs=revise_once(semantic_failures,model=model,packet=packet,
        tool_factory=tool_factory,ask=ask_revision,observations=observations,result=result,table=table,documents=docs)
    for item,proposal,schema,repair_packet in alternatives:
        revised_packet={**repair_packet,'semantic_revision_guard':{
            **item['failure'].feedback(), 'prior_proposal':item['proposal'].model_dump(
                mode='json',exclude={'rationale','requested_probes'})}}
        revised=ProposalBatch(task='T5_revision',proposals=[proposal],
            semantic_schema=SemanticSchema.merged(batch.semantic_schema,schema))
        replacements=_compile(revised,table,goal,docs,result,model=model,packet=revised_packet,
            observations=observations,projection_excluded=projection_excluded,identity_excluded=identity_excluded,
            hierarchy_rules=hierarchy_rules,hierarchy_context=list(observed_hierarchies.values()),
            granularity_rejections=granularity_rejections,identity_requests=requests,
            policy_reviews=policy_reviews,compiled_proposals=compiled_proposals)
        if replacements:
            rules.extend(replacements)
            cid=item['candidate'].candidate_id
            result.resolved_candidate_ids.append(cid)
            result.candidate_replacement_rules[cid]=[r.rule_id for r in replacements]
    # A later sibling can reject an identity already compiled in this batch.
    # Apply that existing judgment to every matching draft without another review.
    rules=[rule for rule in rules if entity_granularity_identity(
        compiled_proposals[rule.rule_id][0]) not in granularity_rejections]
    # A complete alternative in this same batch can explicitly resolve an
    # identity-policy request. No new proposal or second Agent is solicited.
    resolved=set().union(*(policy_reviews.get(rule.rule_id,set()) for rule in rules))
    unresolved=[]
    for proposal,code in failures:
        if (code in HIERARCHY_IDENTITY_REPAIR_CODES and
                canonical(_proposal_repair_record(proposal,code)) in resolved):
            continue
        unresolved.append(code)
    unresolved.extend(request['code'] for request in requests if canonical(request) not in resolved)
    for code in dict.fromkeys(unresolved):
        _issue(result,table,code)
    if unresolved:
        result.stats['deferred_proposals']=result.stats.get('deferred_proposals',0)+len(failures)
    return rules,docs,batch,packet,(projection_excluded,identity_excluded)


def _verify_fd(adapter, table, rule, budget):
    if rule.kind!='column_group_entity' or not rule.attributes.single_valued: return {}
    attributes=list(dict.fromkeys(rule.attributes.single_valued.values()))
    cost=Cost(db_cu=len(attributes)+1)
    reservation='fd_'+rule.rule_id
    budget.reserve(reservation,cost)
    try:
        budget.admit(cost,reservation)
        try: return adapter.check_fd(table,rule.identity.key_columns,attributes)
        finally: budget.charge(cost,reservation)
    finally: budget.release(reservation)


def _bounded_catalog_window(table, tables, packet, model, tool_specs):
    """Keep the task table and fit optional catalog entries into the real prompt."""
    from lightgraph.context import initial_agent_prompt_bytes, STAGE_SUBTASKS
    candidates=[table]+[t for t in tables
        if (t.source_id,t.qualified_name)!=(table.source_id,table.qualified_name)][:63]
    limit=getattr(model.config,'max_prompt_bytes',131072)
    byte_limit=limit
    # The transport conservatively reserves one token per request byte plus
    # output. A larger prompt cap must not pack optional tables past the actual
    # remaining model budget before the first request even starts.
    if getattr(model,'budget',None) is not None:
        allowance=model.budget.snapshot()
        tokens=allowance['limits']['llm_tokens']
        if tokens is not None:
            available=tokens-allowance['used']['llm_tokens']-sum(
                cost['llm_tokens'] for cost in allowance['reserved'].values())
            limit=min(limit,available-getattr(model.config,'max_output_tokens',4096))
    control={'max_steps':getattr(model.config,'max_discovery_steps',8),
             'max_tool_calls':getattr(model.config,'max_tool_calls',16),
             'response_format':getattr(model.config,'response_format','json_schema')}
    def candidate(size):
        return {**packet,
            'catalog':[t.model_dump(mode='json',by_alias=True) for t in candidates[:size]],
            'subtask':(packet.get('subtask') or STAGE_SUBTASKS[packet['task']])+(
                f' Catalog coverage: {size} of {len(tables)} authorized tables are visible '
                'in this call; the task table is included. Unlisted tables have not been examined.')}
    selected=candidate(1)
    required=initial_agent_prompt_bytes(selected,tool_specs,**control)
    if required>byte_limit:raise LightGraphError('model_packet_too_large')
    if required>limit:raise BudgetExceeded('llm_tokens_budget_exhausted')
    # Optional catalog entries must leave room for tools and refreshed evidence.
    # Keep the mandatory task table even if it alone consumes this headroom.
    window_limit=max(required,limit-min(65536,limit//4))
    low,high=1,len(candidates)
    while low<high:
        middle=(low+high+1)//2
        trial=candidate(middle)
        if initial_agent_prompt_bytes(trial,tool_specs,**control)<=window_limit:
            low=middle
            selected=trial
        else:high=middle-1
    return candidates[:low],selected


def _identity_domain_context(rules):
    """Share earlier code-domain hypotheses, never values or publication claims.

    Each later table must obtain its own scoped evidence and independent review.
    Without this small catalog, independent sessions can name the same documented
    domain differently and the executor correctly keeps duplicate interpretations.
    """
    domains=[]
    for rule in rules:
        entries=[]
        if rule.kind=='column_group_entity' and rule.identity.semantic_resolution=='verified_identity_domain':
            entries.append({'entity_kind':rule.identity.entity_kind,'identity_domain':rule.identity.identity_domain,
                'key_columns':[c for c in rule.identity.key_columns if c not in rule.scope.namespaces],
                'namespace_columns':rule.scope.namespaces if rule.identity.identity_scope=='namespace_local' else []})
        if rule.concept_mapping:
            entries.extend({'entity_kind':c.entity_kind,'identity_domain':c.identity_domain,
                'key_columns':[c.key_column],'namespace_columns':rule.scope.namespaces
                    if c.namespace_columns is None else c.namespace_columns}
                for c in rule.concept_mapping.components if c.identity_domain)
        for entry in entries:
            domains.append({**entry,'source':rule.source.model_dump(mode='json',by_alias=True),
                'rule_id':rule.rule_id,'document_versions':rule.dependencies.documents,
                'semantic_schema':rule.semantic_schema.for_usage(entity_kinds={entry['entity_kind']},
                    properties=set(),relations=set()).model_dump(mode='json'),
                'status':'hypothesis_requires_local_review'})
    return {'items':domains[:32],'total':len(domains),'truncated':len(domains)>32}


def discover_with_agent(adapter, goal=None, documents=None, model=None, budget=None, *,
                        table_refs=None,iteration_context=None,revision_rules=None,schema_knowledge=None):
    from .engine import _source, _packet, _understanding, _table_hash, _column_id
    from .tools import ToolExecutor
    goal=goal or ExtractionGoal()
    budget=budget or model.budget
    corpus=documents if isinstance(documents,DocumentCorpus) else DocumentCorpus(documents or [])
    result=DiscoveryResult()
    tables=_catalog(adapter,goal)
    if corpus._catalog is None:
        corpus.bind_catalog(adapter.describe())
    originals={t.qualified_name:t for t in adapter.describe()}
    # Bound each model packet, not the source work list. Otherwise a requested
    # table after the first page can never be explored, even on a retry.
    catalog_size=len(tables)
    window_coverage=[]
    for table in _exploration_order(tables):
        if table_refs is not None and not any(key in table_refs for key in (
            table.qualified_name,table.source_id+'.'+table.qualified_name,
            (table.source_id,table.schema_name,table.name))):continue
        original=originals[table.qualified_name]
        docs=corpus.context_for(_source(table),table.column_names)
        understanding=_understanding(table,[],{},docs)
        understanding.dependency_hash=_table_hash(original)
        result.understandings.append(understanding)
        reservation='agent_validation_'+digest(table.qualified_name)[:16]
        tools=None
        observations=ReviewObservations()
        try:
            # Exploration cannot consume the minimum reserved validation work.
            budget.reserve(reservation,Cost(db_cu=2))
            if len(table.columns)>256: raise LightGraphError('catalog_column_limit')
            packet=_packet(table,goal,[],docs,'T0_discovery',understanding)
            from .schema_knowledge import seed_knowledge
            packet['schema_knowledge']=(schema_knowledge.context(table) if schema_knowledge
                                        else seed_knowledge(table,goal))
            packet['iteration_context']=dict(iteration_context or {})
            from .conflicts import prepare_candidate_context
            prepare_candidate_context(packet,original,goal,corpus)
            packet['iteration_context']['prior_identity_domains']=_identity_domain_context(result.rules)
            prior=[r for r in (revision_rules or []) if r.source==_source(table)]
            original_revision_codes=_iteration_codes(iteration_context,table)
            if prior:
                # The scheduler supplies only affected rules. The full list is
                # available as a typed knowledge summary; one candidate is the
                # immediate revision target, and every replacement is rechecked.
                packet.update(task='T5_revision',revision_of=prior[0].rule_id,
                    revision_candidate=prior[0].model_dump(mode='json',by_alias=True),
                    allowed_revision_codes=original_revision_codes)
                packet['known_conflicts']=[{'code':code,'count':1} for code in packet['allowed_revision_codes'][:32]]
                if 'namespace_scope_missing' in original_revision_codes:
                    packet['subtask']=('修订 iteration_context.namespace_revision 指定规则的身份范围。'
                        '原规则遗漏了候选范围列，需要核实这些列是否限定对象身份及来源权限。'
                        '提交一份完整替换提案，保留原有对象键、层级和属性映射，仅增加有证据的命名空间。'
                        '独立语义审查与全量执行通过后才能替换；不得仅添加一个属性或并存一条新规则。')
            elif iteration_context and iteration_context.get('coverage_columns'):
                packet.update(task='T1_table_discovery',
                    subtask='继续检查 iteration_context.coverage_columns 中尚未解释的列。'
                    'known_rules 含已有规则的身份键、命名空间和每层 code/label/aliases 映射。'
                    '若列是已有对象的英文名、别名或其他属性，可重提保留原有身份与层级的完整提案并补映射；'
                    '程序将作为已有规则的新修订完整验证，不另建实体。不要把属性当作身份键。'
                    '若列表达遗漏的身份范围，提出保留对象键与层级并增加命名空间的完整候选；'
                    '程序会另开 T5 身份修订和独立语义审查，不以属性增补代替范围更改。'
                    '只有独立业务对象才提出新实体；无法说明的列保留未决，不得仅为完成覆盖而排除。')
            tools=ToolExecutor(adapter,goal,budget,documents=corpus,
                allow_source_samples=getattr(model.config,'allow_source_samples',False),allowed_tables=[table])
            window,packet=_bounded_catalog_window(table,tables,packet,model,tools.tool_specs())
            window_coverage.append(len(window))
            tools.close()
            tools=ToolExecutor(adapter,goal,budget,documents=corpus,
                allow_source_samples=getattr(model.config,'allow_source_samples',False),allowed_tables=window)
            batch=_ask(model,packet,tools,observations)
            if schema_knowledge:
                schema_knowledge.record_models(table,getattr(batch,'_runtime_object_models',[]),packet)
            _record_submission_state(result, table, batch)
            docs=tools.context_documents()
            understanding.document_refs=sorted({d['doc_id']+'@'+d['revision'] for d in docs})
            budget.release(reservation)
            if batch.abstain: _issue(result,table,'model_abstained')
            initial_batch=batch
            granularity_rejections={}
            observed_hierarchies={}
            def fresh_tools():
                return ToolExecutor(adapter,goal,budget,documents=corpus,
                    allow_source_samples=getattr(model.config,'allow_source_samples',False),allowed_tables=window)
            def record_revision(revised,repair_packet):
                if schema_knowledge:
                    schema_knowledge.record_models(table,getattr(revised,'_runtime_object_models',[]),repair_packet)
            rules,docs,batch,packet,guards=_compile_candidates(batch,original,goal,docs,result,
                model=model,packet=packet,observations=observations,tool_factory=fresh_tools,
                projection_excluded={entry.column for prior_rule in prior for entry in prior_rule.attributes.excluded},
                hierarchy_rules=prior,granularity_rejections=granularity_rejections,
                observed_hierarchies=observed_hierarchies,on_revision=record_revision)
            if prior and 'fd_violation' in original_revision_codes:
                protected=prior[0]
                safe=[]
                for replacement in rules:
                    added=set(replacement.identity.key_columns)-set(protected.identity.key_columns)
                    attrs=protected.attributes.single_valued
                    if (not set(protected.identity.key_columns)<=set(replacement.identity.key_columns)
                        or added & set(attrs.values())
                        or any(replacement.attributes.single_valued.get(k)!=v for k,v in attrs.items())):
                        _issue(result,table,'revision_masks_counterexample');continue
                    safe.append(replacement)
                rules=safe
            if len(prior)==1 and len(rules)==1:
                rules[0].rule_id=prior[0].rule_id
                rules[0].revision=prior[0].revision+1
            by_id={_column_id(table,c.name):c.name for c in table.columns}
            roles={by_id[m.column_id]:m.role for p in initial_batch.proposals+batch.proposals if p.kind=='column_group'
                   for m in p.claim.members if m.column_id in by_id}
            for column in understanding.columns: column.inferred_role=roles.get(column.name,'unknown')
            for rule in rules:
                checks=_verify_fd(adapter,original,rule,budget)
                if checks.get('violating_groups',0):
                    _issue(result,table,'fd_violation',count=int(checks['violating_groups']))
                    revision={**packet,'task':'T5_revision','known_conflicts':[{'code':'fd_violation',
                        'count':int(checks['violating_groups']),'probe_id':'full_fd_check'}],
                        'probe_digest':[{'probe_id':'full_fd_check','operator':'check_fd',
                            'aggregates':{'violating_groups':int(checks['violating_groups'])}}],
                        'revision_of':rule.rule_id,'revision_candidate':rule.model_dump(mode='json',by_alias=True),
                        'allowed_revision_codes':['fd_violation']}
                    tools=ToolExecutor(adapter,goal,budget,documents=corpus,
                        allow_source_samples=getattr(model.config,'allow_source_samples',False),allowed_tables=window)
                    observations.clear()
                    try:
                        revised_batch=_ask(model,revision,tools,observations)
                        if schema_knowledge is not None:
                            schema_knowledge.record_models(table,getattr(revised_batch,'_runtime_object_models',[]),revision)
                        _record_submission_state(result, table, revised_batch)
                        docs=tools.context_documents()
                    except LightGraphError as exc:
                        # This rule failed FD validation. A failed repair cannot
                        # discard independent siblings already compiled and
                        # reviewed, regardless of their order in the batch.
                        _issue(result,table,exc.code)
                        continue
                    finally:
                        tools.close()
                    fixed,docs,_,revision,guards=_compile_candidates(revised_batch,original,goal,docs,result,
                        model=model,packet=revision,observations=observations,tool_factory=fresh_tools,
                        projection_excluded=guards[0],identity_excluded=guards[1],
                        hierarchy_rules=[*prior,*rules],granularity_rejections=granularity_rejections,
                        observed_hierarchies=observed_hierarchies)
                    accepted=[]
                    for replacement in fixed:
                        added=set(replacement.identity.key_columns)-set(rule.identity.key_columns)
                        attrs=rule.attributes.single_valued
                        # Adding the conflicting label to the key hides the error.
                        if (not added or not set(rule.identity.key_columns)<=set(replacement.identity.key_columns)
                            or added & set(attrs.values())
                            or any(replacement.attributes.single_valued.get(k)!=v for k,v in attrs.items())):
                            _issue(result,table,'revision_masks_counterexample');continue
                        recheck=_verify_fd(adapter,original,replacement,budget)
                        if recheck.get('violating_groups',0) or recheck.get('null_key_rows',0):
                            _issue(result,table,'revision_validation_failed');continue
                        accepted.append(replacement)
                elif checks.get('null_key_rows',0):
                    _issue(result,table,'null_key');accepted=[]
                else: accepted=[rule]
                for candidate in accepted:
                    hypothesis=Hypothesis(hypothesis_id=Hypothesis.identity(candidate.source,candidate.kind,
                        candidate.identity.key_columns),kind=candidate.kind,source=candidate.source,
                        columns=candidate.identity.key_columns,status='proposed',created_by='llm',
                        rule_id=candidate.rule_id,dependencies={'schema':_table_hash(original),
                        'goal':digest(goal.model_dump())})
                    candidate.evidence.hypothesis=hypothesis.hypothesis_id
                    understanding.rule_refs.append(candidate.rule_id)
                    result.hypotheses.append(hypothesis);result.rules.append(candidate)
            if not understanding.rule_refs: _issue(result,table,'no_confirmable_entity_group')
        except LightGraphError as exc:
            _issue(result,table,exc.code)
            if isinstance(exc,BudgetExceeded):break
        finally:
            if tools is not None: tools.close()
            observations.close()
            budget.release(reservation)
    result.stats.update({'tables_considered':len(result.understandings),'catalog_tables':catalog_size,
        'catalog_window_limit':64,'catalog_windows':len(window_coverage),
        'catalog_window_min':min(window_coverage,default=0),
        'catalog_window_max':max(window_coverage,default=0),
        'catalog_windows_partial':sum(size<catalog_size for size in window_coverage),
        'rules_proposed':len(result.rules),
        'model_calls':model.usage['calls'],'discovery_mode':'agent_tools',
        'semantic_validation':'requires_rule_engine','coverage':'discovery_only'})
    result.budget=budget.snapshot()
    return result


def discover_references_with_agent(adapter, validated_rules, documents=None, model=None,
                                   budget=None, goal=None, max_candidates=32,
                                   source_adapters=None, table_refs=None,iteration_context=None,revision_rules=None,
                                   schema_knowledge=None):
    """Check one candidate batch; only missing endpoint context can reopen discovery."""
    from .engine import _source, _packet, _understanding, _table_hash, _column_id
    from .tools import ToolExecutor
    goal=goal or ExtractionGoal();budget=budget or model.budget
    corpus=documents if isinstance(documents,DocumentCorpus) else DocumentCorpus(documents or [])
    source_adapters=dict(source_adapters or {t.source_id:adapter for t in adapter.describe()})
    tables=_catalog(adapter,goal);originals={t.qualified_name:t for t in adapter.describe()}
    all_tables=[t for source in source_adapters.values() for t in source.describe()]
    corpus.bind_catalog(all_tables)
    remote_originals={(t.source_id,t.schema_name,t.name):t for t in all_tables}
    tool_tables=[t for source in source_adapters.values() for t in _catalog(source,goal)]
    approved={r.rule_id:r for r in validated_rules if r.status in ('validated','canary_checked','published')
              and r.validation_ref and r.kind in ('column_group_entity','wide_hierarchy','long_hierarchy')
              and source_in_goal(goal,r.source)}
    result=DiscoveryResult()
    bindings=[{'rule_id':r.rule_id,'source':r.source.model_dump(by_alias=True),
        'entity_kind':r.identity.entity_kind,'level':level,'key_columns':endpoint_columns(r,level)}
        for r in approved.values() for level in (
            [entry.level for entry in r.hierarchy.levels] if r.kind=='wide_hierarchy' else [None])]
    if len(bindings)<2:return result
    if len(tables)>64 or len(approved)>64:
        result.issues.append({'code':'reference_catalog_limit','status':'partial'});return result
    count=0
    for table in tables:
        if table_refs is not None and not any(key in table_refs for key in (
            table.qualified_name,table.source_id+'.'+table.qualified_name,
            (table.source_id,table.schema_name,table.name))):continue
        tools=None
        observations=ReviewObservations()
        issue_start=len(result.issues);rule_start=len(result.rules)
        result.relation_table_outcomes[table.qualified_name]='deferred'
        docs=corpus.context_for(_source(table),table.column_names)
        try:
            if count>=max_candidates:raise LightGraphError('reference_candidate_limit')
            packet=_packet(table,goal,[],docs,'T2_cross_table',_understanding(table,[],{},docs))
            from .schema_knowledge import seed_knowledge,relation_candidates
            packet['schema_knowledge']=(schema_knowledge.context(table) if schema_knowledge
                                        else seed_knowledge(table,goal))
            packet['relation_candidates']=relation_candidates(tool_tables,schema_knowledge,
                goal=goal,max_candidates=min(max_candidates,32))
            packet['iteration_context']=dict(iteration_context or {})
            from .conflicts import prepare_candidate_context
            prepare_candidate_context(packet,originals[table.qualified_name],goal,corpus)
            prior=[r for r in (revision_rules or []) if r.source==_source(table)]
            if prior:
                packet.update(revision_of=prior[0].rule_id,
                    revision_candidate=prior[0].model_dump(mode='json',by_alias=True),
                    allowed_revision_codes=_iteration_codes(iteration_context,table),
                    subtask='复核执行反例中的实体关系映射，保留端点与条件依据；不要把关联缺失伪装为完整覆盖。')
                packet['known_conflicts']=[{'code':code,'count':1} for code in packet['allowed_revision_codes'][:32]]
            if len(tool_tables)>64:raise LightGraphError('reference_catalog_limit')
            packet['catalog']=[t.model_dump(mode='json',by_alias=True) for t in tool_tables]
            packet['authorized_entities']=list(approved)
            packet['semantic_schema']=SemanticSchema.merged(goal.semantic_schema,
                *(r.semantic_schema for r in approved.values())).model_dump(mode='json')
            if len(bindings)>64:raise LightGraphError('reference_catalog_limit')
            packet['entity_bindings']=bindings
            tools=ToolExecutor(adapter,goal,budget,documents=corpus,
                allow_source_samples=getattr(model.config,'allow_source_samples',False),
                allowed_tables=tool_tables,source_adapters=source_adapters)
            batch=_ask(model,packet,tools,observations)
            _record_submission_state(result, table, batch)
            docs=tools.context_documents()
            # A final proposal can name an endpoint without using a read tool.
            # Backend verification is a read too: first let the model reconsider
            # with every endpoint's applicable context, before querying it.
            authorized_tables={(t.source_id,t.schema_name,t.name):t for t in tool_tables}
            for context_round in range(3):
                involved={(ref['source_id'],ref.get('schema','public'),ref['table'])
                          for ref in tools.context_tables()}
                for proposal in batch.proposals:
                    if proposal.kind!='reference':continue
                    for endpoint in (proposal.claim.source_entity,proposal.claim.target_entity):
                        binding=approved.get(endpoint)
                        if binding:
                            involved.add((binding.source.source_id,binding.source.schema_name,binding.source.table))
                needed=corpus.contexts_for([authorized_tables[key] for key in sorted(involved)
                                           if key in authorized_tables])
                def section_keys(sections):
                    return {(d['doc_id'],d['revision'],d['section_id']) for d in sections}
                if section_keys(needed)<=section_keys(docs):break
                if context_round==2:raise LightGraphError('endpoint_context_revision_limit')
                packet={**packet,'document_snippets':needed,
                        'context_tables':[_source(authorized_tables[key]).model_dump(mode='json',by_alias=True)
                                          for key in sorted(involved) if key in authorized_tables],
                        'subtask':'依据新增的端点表说明重新判断关系；不满足说明中的条件时撤回提案。'}
                tools.close()
                tools=ToolExecutor(adapter,goal,budget,documents=corpus,
                    allow_source_samples=getattr(model.config,'allow_source_samples',False),
                    allowed_tables=tool_tables,source_adapters=source_adapters)
                batch=_ask(model,packet,tools,observations)
                _record_submission_state(result, table, batch)
                docs=tools.context_documents()
            if batch.abstain:_issue(result,table,'model_abstained')
            elif not batch.proposals:_issue(result,table,'no_confirmable_relation')
            lookup={_column_id(table,c.name):c.name for c in table.columns}
            for proposal in batch.proposals:
                if proposal.kind!='reference':
                    _issue(result,table,'discriminator_requires_registered_selectors');continue
                count+=1
                if count>max_candidates:raise LightGraphError('reference_candidate_limit')
                claim=proposal.claim
                try:
                    local=[lookup[c] for c in claim.source_columns];remote=[lookup[c] for c in claim.target_columns]
                    if any(column_is_evidence_only(goal,table,c) for c in local+remote):
                        raise LightGraphError('evidence_only_column_projected')
                    source=approved.get(claim.source_entity)
                    if source is None:
                        matches=[r for r in approved.values() if r.source==_source(table)
                            and any(binding['rule_id']==r.rule_id and binding['level']==claim.source_level
                                    and binding['key_columns']==local for binding in bindings)]
                        if len(matches)!=1:raise LightGraphError('reference_source_binding_ambiguous')
                        source=matches[0]
                    target=approved[claim.target_entity]
                    if claim.condition_ref or claim.polymorphic_by:raise LightGraphError('reference_condition_requires_registered_selector')
                    if goal.relation_kinds and claim.relation_kind not in goal.relation_kinds:raise LightGraphError('outside_relation_goal')
                    schema=SemanticSchema.merged(goal.semantic_schema,batch.semantic_schema,
                        source.semantic_schema,target.semantic_schema)
                    schema=_reference_schema(schema,source.identity.entity_kind,target.identity.entity_kind,
                        claim.relation_kind,goal)
                    source_keys=endpoint_columns(source,claim.source_level)
                    target_keys=endpoint_columns(target,claim.target_level)
                    relation_contract=_reference_relation_contract(schema,claim.relation_kind)
                    # Identical rule, occurrence level and ordered mapping can
                    # only resolve to the same entity. Reject that closed-contract
                    # violation before source probes or a paid semantic review.
                    # Different levels/mappings still need ordinary validation.
                    if (source.rule_id==target.rule_id and claim.source_level==claim.target_level
                            and local==remote and not relation_contract['allow_self_links']):
                        raise LightGraphError('reference_self_link_forbidden')
                    endpoint_checks=[]
                    for binding,columns,keys in ((source,local,source_keys),(target,remote,target_keys)):
                        destination=remote_originals.get((binding.source.source_id,binding.source.schema_name,binding.source.table))
                        target_adapter=source_adapters.get(binding.source.source_id)
                        if destination is None or target_adapter is None:raise LightGraphError('unavailable_reference_target')
                        if len(columns)!=len(keys):raise LightGraphError('reference_arity_mismatch')
                        cross_source=binding.source.source_id!=table.source_id
                        cost=Cost(db_cu=6 if cross_source else 1)
                        budget.admit(cost)
                        try:
                            if cross_source:
                                from lightgraph.cross_source import check_cross_source_reference
                                check=check_cross_source_reference(adapter,originals[table.qualified_name],columns,
                                    target_adapter,destination,keys,
                                    max_working_bytes=budget.limits.memory_bytes)
                                if not check['complete'] or not check['consistent']:
                                    raise LightGraphError('cross_source_reference_incomplete')
                            else:
                                check=adapter.check_reference(originals[table.qualified_name],columns,destination,keys)
                        finally:budget.charge(cost)
                        if check.get('partial_null_refs',0):raise LightGraphError('reference_partial_null')
                        if claim.require_full_coverage and check.get('null_refs',0):
                            raise LightGraphError('reference_null_endpoint')
                        if any(check.get(k,0) for k in ('unmatched','ambiguous')):raise LightGraphError('reference_missing')
                        if not check.get('unique_matches',0):raise LightGraphError('reference_no_populated_endpoint')
                        endpoint_checks.append(check)
                    review_packet={**packet,'semantic_schema':schema.model_dump(mode='json'),
                        'probe_digest':list(packet.get('probe_digest',[]))+[{
                        'probe_id':'backend_endpoint_verification','operator':'check_reference',
                        'aggregates':{field:sum(check.get(field,0) for check in endpoint_checks)
                            for field in ('unmatched','null_refs','partial_null_refs','ambiguous','unique_matches')}}]}
                    # The provider's proposal is not its own semantic evidence.
                    # Review all endpoint context after structural verification.
                    evidence=review_semantics(model,review_packet,{
                        'kind':'reference','scope':packet['scope'],
                        'relation_definition':relation_contract,
                        'claim':{**claim.model_dump(mode='json'),'source_entity':source.rule_id}},docs,
                        observations=observations)
                    if relation_contract['allow_self_links']:
                        # Only this exact, independently supported contract
                        # grants the executor permission for reflexive edges.
                        evidence=[*evidence,'semantic_review:allow_self_links']
                    rule_id='rule_'+digest(table.source_id,table.qualified_name,source.rule_id,target.rule_id,
                        claim.source_level,claim.target_level,claim.relation_kind,local,remote)[:24]
                    replacement=[r for r in prior if r.relation and
                        r.relation.source_rule_id==source.rule_id and r.relation.target_rule_id==target.rule_id
                        and r.relation.source_level==claim.source_level and r.relation.target_level==claim.target_level
                        and r.relation.relation==claim.relation_kind]
                    compiled=RuleSpec(rule_id=replacement[0].rule_id if len(replacement)==1 else rule_id,
                        revision=replacement[0].revision+1 if len(replacement)==1 else 1,
                        semantic_schema=schema.for_usage(entity_kinds={source.identity.entity_kind,target.identity.entity_kind},
                            relations={claim.relation_kind}),
                        kind='reference',goal_refs=[goal.goal_id],source=_source(table),
                        scope=ScopeSpec(namespaces=[local[source_keys.index(c)]
                            for c in source.scope.namespaces if c in source_keys]),
                        identity=IdentitySpec(key_columns=local,entity_kind=source.identity.entity_kind),
                        relation=RelationSpec(source_rule_id=source.rule_id,target_rule_id=target.rule_id,
                            source_columns=local,target_columns=remote,relation=claim.relation_kind,
                            source_level=claim.source_level,target_level=claim.target_level,
                            require_full_coverage=claim.require_full_coverage),
                        dependencies=Dependencies(schema=_table_hash(originals[table.qualified_name]),
                            documents=sorted({d['doc_id']+'@'+d['revision'] for d in docs})),
                        evidence=rule_evidence(evidence,reviewed=True,documents=docs),coverage=Coverage(scope='full',consistent=False))
                    _preflight(compiled,goal)
                    if not any(old.rule_id==compiled.rule_id for old in result.rules):
                        result.rules.append(compiled)
                except BudgetExceeded:raise
                except LightGraphError as exc:
                    _issue(result,table,exc.code)
                    if isinstance(exc, SemanticReviewFailure):
                        result.pending_candidates.append(candidate_record(proposal,table,review_packet,docs,exc))
                    elif exc.code in SEMANTIC_REVIEW_PROTOCOL_CODES | REPAIRABLE_PROPOSAL_CODES:
                        from types import SimpleNamespace
                        from typing import get_args
                        from .models import PendingCandidate
                        failure=SimpleNamespace(code=exc.code if exc.code in SEMANTIC_REVIEW_PROTOCOL_CODES
                            else 'reference_contract_unresolved',blocking=[],required=[],stage='reference')
                        candidate=candidate_record(proposal,table,packet,docs,failure)
                        allowed=set(get_args(PendingCandidate.model_fields['failure_details'].annotation.__args__[0]))
                        if exc.code in allowed:candidate.failure_details=[exc.code]
                        result.pending_candidates.append(candidate)
                    if exc.code in REPAIRABLE_PROPOSAL_CODES:
                        result.stats['deferred_proposals']=result.stats.get('deferred_proposals',0)+1
        except LightGraphError as exc:
            _issue(result,table,exc.code)
            if isinstance(exc,BudgetExceeded):break
        finally:
            table_issues=result.issues[issue_start:]
            if any(issue.get('status')=='deferred' for issue in table_issues):
                result.relation_table_outcomes[table.qualified_name]='deferred'
            elif table_issues or len(result.rules)==rule_start:
                result.relation_table_outcomes[table.qualified_name]='unresolved'
            else:result.relation_table_outcomes[table.qualified_name]='resolved'
            if tools is not None:tools.close()
            observations.close()
    result.stats.update({'reference_candidates':count,'rules_proposed':len(result.rules),'endpoint_rules':len(approved)})
    result.budget=budget.snapshot()
    return result
