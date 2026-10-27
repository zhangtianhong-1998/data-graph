"""Agent discovery produces rules; complete rule execution alone may publish them.

Only schema references, roles, counts and fixed issue codes escape this module.
The Agent and query tools own ephemeral observations and never persist transcripts.
"""
from __future__ import annotations

from lightgraph.contracts import (ExtractionGoal, ExcludedColumn, RuleSpec, IdentitySpec,
    ScopeSpec, RelationSpec, Dependencies, Coverage, digest)
from lightgraph.documents import DocumentCorpus
from lightgraph.errors import LightGraphError, BudgetExceeded
from .budget import BudgetAllocator, Cost
from .goal import source_in_goal, column_is_excluded, column_is_evidence_only
from .models import DiscoveryResult, Hypothesis, ProposalBatch
from .semantics import declared_evidence, review_semantics, rule_evidence
from .observations import ReviewObservations
from lightgraph.semantic_schema import SemanticSchema, EntityTypeDefinition
from lightgraph.rules.endpoints import endpoint_columns
from .repair import (REPAIRABLE_PROPOSAL_CODES, SEMANTIC_REVIEW_PROTOCOL_CODES,
    PROPOSAL_REPAIR_GUIDANCE, MAX_PROPOSAL_REPAIR_ROUNDS)


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


def _compile(batch, table, goal, docs, result, *, model=None,packet=None,observations=None,failures=None,
             projection_excluded=(),identity_excluded=(),conflicted_ids=None):
    from .engine import _compile_model, SENSITIVE
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
    rules=[]
    for p in batch.proposals:
        if p.kind=='exclusion': continue
        try:
            if goal.entity_kinds and getattr(p.claim,'entity_kind_hint',None) not in goal.entity_kinds:
                raise LightGraphError('outside_extraction_goal')
            schema=SemanticSchema.merged(goal.semantic_schema,batch.semantic_schema)
            local=ProposalBatch(task=batch.task,proposals=exclusions+[p],semantic_schema=schema)
            review=None
            sharing_requested=getattr(p.claim,'identity_resolution','source_scoped')=='shared_definition'
            if model is not None and (p.kind in ('long_hierarchy','wide_hierarchy') or sharing_requested):
                review=review_semantics(model,{**(packet or {}),'semantic_schema':schema.model_dump(mode='json')},{
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
                if rule.kind=='column_group_entity' and sharing and rule.scope.namespaces and rule.attributes.single_valued.get('definition'):
                    rule.identity.semantic_resolution='verified_definition_registry'
                    # The goal defines the inferred registry's namespace; a
                    # declaration continues to use its immutable document ID.
                    registry=('goal:'+goal.goal_id if sharing_requested else sharing[0].split('@',1)[0])
                    rule.identity.semantic_namespace='declared_definition:'+registry+':'+rule.identity.entity_kind
                    rule.evidence=rule_evidence(sharing,reviewed=bool(review),documents=docs)
                elif sharing_requested:
                    raise LightGraphError('shared_definition_attributes_missing')
                _preflight(rule,goal)
                rules.append(rule)
        except LightGraphError as exc:
            if failures is not None and exc.code in REPAIRABLE_PROPOSAL_CODES:
                failures.append((p,exc.code))
            else:_issue(result,table,exc.code)
    unique={}
    conflicts=set()
    for rule in rules:
        previous=unique.get(rule.rule_id)
        if previous is not None and previous!=rule:
            conflicts.add(rule.rule_id);_issue(result,table,'conflicting_rule_proposals')
        unique[rule.rule_id]=rule
    if conflicted_ids is not None:conflicted_ids.update(conflicts)
    return [rule for rid,rule in unique.items() if rid not in conflicts]


def _repair_packet(packet, batch, failures, round_number, *, task='T5_revision'):
    """Only typed schema claims and fixed diagnostics enter a fresh Agent task."""
    repairs=[]
    for proposal,code in failures:
        clean=proposal.model_copy(update={'rationale':'','requested_probes':[]})
        repairs.append({'code':code,'guidance':PROPOSAL_REPAIR_GUIDANCE[code],
                        'proposal':clean.model_dump(mode='json')})
    codes=sorted({code for _,code in failures})
    return {**packet,'task':task,'semantic_schema':batch.semantic_schema.model_dump(mode='json'),
        'proposal_repair_round':round_number,'proposal_repairs':repairs,
        'allowed_revision_codes':sorted(set(packet.get('allowed_revision_codes',[])) | set(codes)),
        'known_conflicts':[{'code':code,'count':sum(value==code for _,value in failures)} for code in codes],
        'subtask':'Repair only the rejected proposals described in proposal_repairs. Previously compiled independent groups are retained. Preserve the goal, exclusions, observed meaning and issued references. Fix contracts or abstain; a compiler failure does not justify weakening semantic evidence. Supply all definitions needed by your repaired proposals.'}


def _compile_with_repairs(batch,table,goal,docs,result,*,model,packet,observations,tool_factory,
                          projection_excluded=(),identity_excluded=()):
    """Keep valid siblings while giving malformed proposals bounded fresh tasks."""
    from lightgraph.contracts import canonical
    from lightgraph.rules import RuleEngine
    accepted={};blocked=set();seen=set();round_number=0
    projection_excluded=set(projection_excluded);identity_excluded=set(identity_excluded)
    while True:
        projection,identity=_proposal_guards(batch,table,goal)
        projection_excluded.update(projection);identity_excluded.update(identity)
        for rule_id,rule in list(accepted.items()):
            if (set(RuleEngine.columns(None,rule)) & projection_excluded
                or _identity_columns(rule) & identity_excluded):
                del accepted[rule_id]
                _issue(result,table,'proposal_exclusion_conflict')
        failures=[]
        rules=_compile(batch,table,goal,docs,result,model=model,packet=packet,
            observations=observations,failures=failures,projection_excluded=projection_excluded,
            identity_excluded=identity_excluded,conflicted_ids=blocked)
        for rule_id in blocked:accepted.pop(rule_id,None)
        for rule in rules:
            if rule.rule_id in blocked:continue
            if rule.rule_id not in accepted:
                accepted[rule.rule_id]=rule
                if round_number:result.stats['recovered_proposals']=result.stats.get('recovered_proposals',0)+1
            elif accepted[rule.rule_id]!=rule:
                _issue(result,table,'conflicting_rule_proposals')
                del accepted[rule.rule_id];blocked.add(rule.rule_id)
        if not failures:break
        signature=canonical([batch.semantic_schema.model_dump(mode='json'),[
            (proposal.kind,proposal.claim.model_dump(mode='json'),code) for proposal,code in failures]])
        if round_number>=MAX_PROPOSAL_REPAIR_ROUNDS or signature in seen:
            for _,code in failures:_issue(result,table,code)
            result.stats['deferred_proposals']=result.stats.get('deferred_proposals',0)+len(failures)
            break
        seen.add(signature);round_number+=1
        packet=_repair_packet(packet,batch,failures,round_number)
        result.stats['compile_repair_rounds']=result.stats.get('compile_repair_rounds',0)+1
        observations.clear()
        tools=tool_factory()
        try:
            batch=_ask(model,packet,tools,observations)
            docs=tools.context_documents()
        except LightGraphError as exc:
            _issue(result,table,exc.code)
            for _,code in failures:_issue(result,table,code)
            result.stats['deferred_proposals']=result.stats.get('deferred_proposals',0)+len(failures)
            break
        finally:tools.close()
        if batch.abstain:
            _issue(result,table,'model_abstained')
            for _,code in failures:_issue(result,table,code)
            result.stats['deferred_proposals']=result.stats.get('deferred_proposals',0)+len(failures)
            break
    return list(accepted.values()),docs,batch,packet,(projection_excluded,identity_excluded)


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
    limit=getattr(model.config,'max_prompt_bytes',65536)
    def candidate(size):
        return {**packet,
            'catalog':[t.model_dump(mode='json',by_alias=True) for t in candidates[:size]],
            'subtask':STAGE_SUBTASKS[packet['task']]+(
                f' Catalog coverage: {size} of {len(tables)} authorized tables are visible '
                'in this call; the task table is included. Unlisted tables have not been examined.')}
    selected=candidate(1)
    if initial_agent_prompt_bytes(selected,tool_specs)>limit:
        raise LightGraphError('model_packet_too_large')
    low,high=1,len(candidates)
    while low<high:
        middle=(low+high+1)//2
        trial=candidate(middle)
        if initial_agent_prompt_bytes(trial,tool_specs)<=limit:
            low=middle
            selected=trial
        else:high=middle-1
    return candidates[:low],selected


def discover_with_agent(adapter, goal=None, documents=None, model=None, budget=None, *,
                        table_refs=None,iteration_context=None,revision_rules=None):
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
    for table in tables:
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
            packet['iteration_context']=dict(iteration_context or {})
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
            tools=ToolExecutor(adapter,goal,budget,documents=corpus,
                allow_source_samples=getattr(model.config,'allow_source_samples',False),allowed_tables=[table])
            window,packet=_bounded_catalog_window(table,tables,packet,model,tools.tool_specs())
            window_coverage.append(len(window))
            tools.close()
            tools=ToolExecutor(adapter,goal,budget,documents=corpus,
                allow_source_samples=getattr(model.config,'allow_source_samples',False),allowed_tables=window)
            batch=_ask(model,packet,tools,observations)
            docs=tools.context_documents()
            understanding.document_refs=sorted({d['doc_id']+'@'+d['revision'] for d in docs})
            budget.release(reservation)
            if batch.abstain: _issue(result,table,'model_abstained')
            initial_batch=batch
            def fresh_tools():
                return ToolExecutor(adapter,goal,budget,documents=corpus,
                    allow_source_samples=getattr(model.config,'allow_source_samples',False),allowed_tables=window)
            rules,docs,batch,packet,guards=_compile_with_repairs(batch,original,goal,docs,result,
                model=model,packet=packet,observations=observations,tool_factory=fresh_tools,
                projection_excluded={entry.column for prior_rule in prior for entry in prior_rule.attributes.excluded})
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
                    revised_batch=_ask(model,revision,tools,observations)
                    docs=tools.context_documents()
                    fixed,docs,_,revision,guards=_compile_with_repairs(revised_batch,original,goal,docs,result,
                        model=model,packet=revision,observations=observations,tool_factory=fresh_tools,
                        projection_excluded=guards[0],identity_excluded=guards[1])
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
                                   source_adapters=None, table_refs=None,iteration_context=None,revision_rules=None):
    """Resolve differently named local endpoint columns to verified entity keys."""
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
        docs=corpus.context_for(_source(table),table.column_names)
        try:
            if count>=max_candidates:raise LightGraphError('reference_candidate_limit')
            packet=_packet(table,goal,[],docs,'T2_cross_table',_understanding(table,[],{},docs))
            packet['iteration_context']=dict(iteration_context or {})
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
            docs=tools.context_documents()
            repair_round=0;seen_repairs=set()
            while True:
                failures=[]
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
                    docs=tools.context_documents()
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
                            if any(check.get(k,0) for k in ('unmatched','null_refs','ambiguous')):raise LightGraphError('reference_missing')
                        review_packet={**packet,'semantic_schema':schema.model_dump(mode='json'),
                            'probe_digest':list(packet.get('probe_digest',[]))+[{
                            'probe_id':'backend_endpoint_verification','operator':'check_reference',
                            'aggregates':{'unmatched':0,'null_refs':0,'ambiguous':0}}]}
                        # The provider's proposal is not its own semantic evidence.
                        # Review all endpoint context after structural verification.
                        evidence=review_semantics(model,review_packet,{
                            'kind':'reference','scope':packet['scope'],
                            'claim':{**claim.model_dump(mode='json'),'source_entity':source.rule_id}},docs,
                            observations=observations)
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
                                source_level=claim.source_level,target_level=claim.target_level),
                            dependencies=Dependencies(schema=_table_hash(originals[table.qualified_name]),
                                documents=sorted({d['doc_id']+'@'+d['revision'] for d in docs})),
                            evidence=rule_evidence(evidence,reviewed=True,documents=docs),coverage=Coverage(scope='full',consistent=False))
                        _preflight(compiled,goal)
                        if not any(old.rule_id==compiled.rule_id for old in result.rules):
                            result.rules.append(compiled)
                            if repair_round:result.stats['recovered_proposals']=result.stats.get('recovered_proposals',0)+1
                    except BudgetExceeded:raise
                    except LightGraphError as exc:
                        if exc.code in REPAIRABLE_PROPOSAL_CODES:failures.append((proposal,exc.code))
                        else:_issue(result,table,exc.code)
                if not failures:break
                from lightgraph.contracts import canonical
                signature=canonical([batch.semantic_schema.model_dump(mode='json'),[
                    (proposal.kind,proposal.claim.model_dump(mode='json'),code) for proposal,code in failures]])
                if repair_round>=MAX_PROPOSAL_REPAIR_ROUNDS or signature in seen_repairs:
                    for _,code in failures:_issue(result,table,code)
                    result.stats['deferred_proposals']=result.stats.get('deferred_proposals',0)+len(failures)
                    break
                seen_repairs.add(signature);repair_round+=1
                packet=_repair_packet(packet,batch,failures,repair_round,task='T2_cross_table')
                result.stats['compile_repair_rounds']=result.stats.get('compile_repair_rounds',0)+1
                observations.clear();tools.close()
                tools=ToolExecutor(adapter,goal,budget,documents=corpus,
                    allow_source_samples=getattr(model.config,'allow_source_samples',False),
                    allowed_tables=tool_tables,source_adapters=source_adapters)
                try:
                    batch=_ask(model,packet,tools,observations)
                    docs=tools.context_documents()
                except LightGraphError:
                    for _,code in failures:_issue(result,table,code)
                    result.stats['deferred_proposals']=result.stats.get('deferred_proposals',0)+len(failures)
                    raise
                if batch.abstain:
                    _issue(result,table,'model_abstained')
                    for _,code in failures:_issue(result,table,code)
                    result.stats['deferred_proposals']=result.stats.get('deferred_proposals',0)+len(failures)
                    break
        except LightGraphError as exc:
            _issue(result,table,exc.code)
            if isinstance(exc,BudgetExceeded):break
        finally:
            if tools is not None:tools.close()
            observations.close()
    result.stats.update({'reference_candidates':count,'rules_proposed':len(result.rules),'endpoint_rules':len(approved)})
    result.budget=budget.snapshot()
    return result
