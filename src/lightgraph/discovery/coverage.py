"""Bounded follow-up for authorized columns not yet explained by any rule."""
from __future__ import annotations

from lightgraph.contracts import SourceRef, digest
from lightgraph.discovery.goal import column_is_excluded, column_is_evidence_only
from lightgraph.runtime.iteration import rule_shape


def attribute_enrichment(previous, candidate):
    """Merge presentation attributes without changing identity or topology.

    Defining properties (formula, unit, grain, etc.) can alter semantic identity
    even when physical keys stay unchanged. They require ordinary revision,
    not this coverage-only path. Existing mappings and exclusions are retained.
    """
    from lightgraph.identity.registry import STRONG_FIELDS
    merged=candidate.model_copy(deep=True)
    if previous.attributes.excluded!=candidate.attributes.excluded:
        return None
    old=previous.attributes
    merged.attributes=old.model_copy(deep=True)
    for cardinality in ('single_valued','multi_valued'):
        target=getattr(merged.attributes,cardinality)
        other=getattr(merged.attributes,'multi_valued' if cardinality=='single_valued' else 'single_valued')
        for name,column in getattr(candidate.attributes,cardinality).items():
            if name in other or (name in target and target[name]!=column):return None
            if name not in target and name in STRONG_FIELDS:return None
            target[name]=column
    if bool(previous.hierarchy)!=bool(candidate.hierarchy):return None
    if previous.hierarchy:
        if len(previous.hierarchy.levels)!=len(candidate.hierarchy.levels):return None
        for before,after in zip(previous.hierarchy.levels,merged.hierarchy.levels):
            if (before.level,before.code)!=(after.level,after.code):return None
            if before.label and after.label and before.label!=after.label:return None
            after.label=before.label or after.label
            after.aliases=list(dict.fromkeys([*before.aliases,*after.aliases]))
    # Compare the rest of the complete executable contract after removing only
    # the enrichment surfaces. In particular scope, relations and definitions
    # cannot change under the guise of filling an unexplained column.
    comparable=merged.model_copy(deep=True)
    comparable.attributes=previous.attributes
    if comparable.hierarchy:
        for before,after in zip(previous.hierarchy.levels,comparable.hierarchy.levels):
            after.label=before.label;after.aliases=list(before.aliases)
    if rule_shape(comparable)!=rule_shape(previous):return None
    merged.revision=previous.revision+1
    return merged


def namespace_refinement(previous, candidate):
    """Identify a scope-only refinement; authorization requires a separate review.

    A different namespace normally changes the generated rule ID. Match the
    complete remaining contract so this correction cannot create a second,
    overlapping public interpretation or hide a change in object granularity.
    """
    if previous.kind not in ('wide_hierarchy','long_hierarchy','column_group_entity'):
        return None
    old=set(previous.scope.namespaces);new=set(candidate.scope.namespaces)
    if not old < new or len(new)!=len(candidate.scope.namespaces):return None
    added=new-old
    if not added<=set(candidate.identity.key_columns):return None
    if [c for c in candidate.identity.key_columns if c not in added]!=previous.identity.key_columns:
        return None
    comparable=candidate.model_copy(deep=True)
    comparable.rule_id=previous.rule_id
    comparable.scope.namespaces=list(previous.scope.namespaces)
    comparable.identity.key_columns=list(previous.identity.key_columns)
    enriched=attribute_enrichment(previous,comparable)
    if enriched is None:return None
    enriched.scope.namespaces=list(candidate.scope.namespaces)
    enriched.identity.key_columns=list(candidate.identity.key_columns)
    return enriched


def unexplained_columns(table, rules, understandings, goal, engine):
    source=SourceRef(source_id=table.source_id,schema=table.schema_name,table=table.name)
    covered=set()
    for rule in rules:
        if rule.source==source:
            covered.update(engine.columns(rule))
            covered.update(e.column for e in rule.attributes.excluded)
    # Explicitly excluded observations/order/audit fields are an explanation,
    # but a guessed entity role without a valid rule is not coverage.
    for understanding in understandings:
        if understanding.source==source:
            covered.update(c.name for c in understanding.columns
                if c.inferred_role in ('fact_value','display_order','audit'))
    return [c for c in table.column_names if c not in covered
        and not column_is_excluded(goal,table,c) and not column_is_evidence_only(goal,table,c)]


def discover_uncovered(*,tables,contexts,adapters,goal,engine,rules,understandings,
                       ledger,corpus,model,max_rounds,execute,validated_rules,explore=None,candidate_queue=None):
    """Retain siblings, ask only about remaining columns, stop at no progress.

    Returns safe issues/stats plus source references of unfinished work. The
    caller owns publication and executes each new rule through its usual gate.
    """
    from . import discover
    explore=explore or (lambda function,*args,**kwargs:function(*args,**kwargs))
    from lightgraph.pipeline import _deferred_tables
    issues=[];stats={'coverage_discovery_tasks':0};unfinished=set()
    scope_attempts=set()
    if not model or not callable(getattr(model,'explore',None)):
        return issues,stats,unfinished
    for table in tables:
        if table.qualified_name not in contexts.get(table.source_id,{}):continue
        source=SourceRef(source_id=table.source_id,schema=table.schema_name,table=table.name)
        local=[r for r in validated_rules if r.source==source]
        # An entirely unmodeled table is retried by metadata discovery; do not
        # ask the same question a second time within one run.
        if not local:continue
        remaining=unexplained_columns(table,validated_rules,understandings,goal,engine)
        if not remaining:
            ledger.resolve_exploration(source,'coverage_discovery')
            continue
        version='coverage_'+digest(contexts[table.source_id][table.qualified_name],remaining,
                                   sorted((r.rule_id,r.revision) for r in local))
        item=ledger.enqueue(source,task='coverage_discovery',rules=local,context_version=version,force=True)
        ledger.supersede_previous(item)
        for iteration in range(1,max_rounds):
            ledger.transition(item,'running')
            context=ledger.context([item],rules,iteration=iteration)
            context['coverage_columns']=remaining
            result=explore(discover,adapters[table.source_id],goal,documents=corpus,model=model,
                table_refs={table.qualified_name},iteration_context=context)
            def record(result):
                ledger.record_discovery_feedback([source],result.issues,task='coverage_discovery')
                if candidate_queue is not None:candidate_queue.record(result)
                stats['coverage_discovery_tasks']+=1
                for key in ('compile_repair_rounds','recovered_proposals','deferred_proposals',
                            'discovery_invalid_drafts','discovery_pending_submissions','semantic_revision_attempts'):
                    stats[key]=stats.get(key,0)+int(result.stats.get(key,0))
                issues.extend(result.issues)
                understandings.extend(result.understandings)
            record(result)
            deferred=bool(_deferred_tables(result,{table.qualified_name}))
            existing={r.rule_id:r for r in rules}
            for rule in result.rules:
                targets=[prior for prior in local if namespace_refinement(prior,rule) is not None]
                if targets:
                    # Never publish the differently hashed candidate alongside
                    # its old unscoped rule. The Agent must explicitly revise
                    # one prior interpretation and pass an independent review.
                    issue={'code':'namespace_scope_missing','table':table.qualified_name,'status':'unresolved'}
                    if len(targets)!=1 or targets[0].rule_id in scope_attempts:
                        issues.append(issue);continue
                    previous=targets[0];scope_attempts.add(previous.rule_id)
                    work=ledger.enqueue(source,task='review_pattern',codes=['namespace_scope_missing'],
                        rules=[previous],context_version=contexts[table.source_id][table.qualified_name],
                        column_refs=rule.scope.namespaces,force=True)
                    ledger.transition(work,'running')
                    revision_context=ledger.context([work],rules,iteration=iteration)
                    revision_context['namespace_revision']={'rule_id':previous.rule_id,
                        'previous_namespace_columns':previous.scope.namespaces,
                        'proposed_namespace_columns':rule.scope.namespaces,
                        'required_action':'Verify the scope meaning and submit one complete replacement; preserve object keys and topology.'}
                    revised=explore(discover,adapters[table.source_id],goal,documents=corpus,model=model,
                        table_refs={table.qualified_name},iteration_context=revision_context,revision_rules=[previous])
                    record(revised)
                    accepted=None
                    if len(revised.rules)==1:
                        candidate=revised.rules[0]
                        if (candidate.rule_id==previous.rule_id
                            and candidate.scope.namespaces==rule.scope.namespaces
                            and candidate.evidence.semantic_decision=='model_review'
                            and candidate.evidence.semantic_status=='supported'):
                            accepted=namespace_refinement(previous,candidate)
                    if accepted is not None and execute(accepted,preserve_previous=True):
                        rules[:]=[r for r in rules if r.rule_id!=previous.rule_id]+[accepted]
                        local[:]=[r for r in local if r.rule_id!=previous.rule_id]+[accepted]
                        existing[previous.rule_id]=accepted
                        stats['coverage_identity_revisions']=stats.get('coverage_identity_revisions',0)+1
                        ledger.transition(work,'resolved')
                    else:
                        state='deferred' if _deferred_tables(revised,{table.qualified_name}) else 'unresolved'
                        ledger.transition(work,state);issue['status']=state;issues.append(issue)
                        ledger.record_discovery_feedback([source],[*revised.issues,issue],task='coverage_discovery')
                        deferred=True
                    continue
                if rule.rule_id in existing:
                    previous=existing[rule.rule_id]
                    enrichment=attribute_enrichment(previous,rule)
                    if enrichment is None:
                        issues.append({'code':'coverage_existing_rule_conflict','table':table.qualified_name,'status':'unresolved'})
                    elif rule_shape(enrichment)!=rule_shape(previous):
                        if execute(enrichment,preserve_previous=True):
                            rules[:]=[r for r in rules if r.rule_id!=rule.rule_id]+[enrichment]
                            existing[rule.rule_id]=enrichment
                            stats['coverage_attribute_revisions']=stats.get('coverage_attribute_revisions',0)+1
                    continue
                rules.append(rule);existing[rule.rule_id]=rule
                execute(rule)
            after=unexplained_columns(table,validated_rules,understandings,goal,engine)
            if deferred:
                # Compilation retained independent valid siblings. Run their
                # normal publication gates before leaving other work deferred.
                # A partial batch still cannot certify exploration complete.
                remaining=after
                unfinished.add((table.source_id,table.qualified_name))
                ledger.transition(item,'deferred');break
            if not after:
                ledger.transition(item,'resolved');remaining=[];break
            if after==remaining:
                ledger.transition(item,'unresolved');break
            remaining=after
            # Continue only if the set of unexplained columns actually shrank.
            ledger.transition(item,'deferred')
        else:ledger.transition(item,'deferred')
        if remaining:
            unfinished.add((table.source_id,table.qualified_name))
            issues.append({'code':'unexplained_columns','table':table.qualified_name,
                           'count':len(remaining),'status':item.state})
    return issues,stats,unfinished
