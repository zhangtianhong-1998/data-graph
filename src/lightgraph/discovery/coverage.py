"""Bounded follow-up for authorized columns not yet explained by any rule."""
from __future__ import annotations

from lightgraph.contracts import SourceRef, digest
from lightgraph.discovery.goal import column_is_excluded, column_is_evidence_only
from lightgraph.runtime.iteration import rule_shape


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
                       ledger,corpus,model,max_rounds,execute,validated_rules):
    """Retain siblings, ask only about remaining columns, stop at no progress.

    Returns safe issues/stats plus source references of unfinished work. The
    caller owns publication and executes each new rule through its usual gate.
    """
    from . import discover
    from lightgraph.pipeline import _deferred_tables
    issues=[];stats={'coverage_discovery_tasks':0};unfinished=set()
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
            result=discover(adapters[table.source_id],goal,documents=corpus,model=model,
                table_refs={table.qualified_name},iteration_context=context)
            stats['coverage_discovery_tasks']+=1
            for key in ('compile_repair_rounds','recovered_proposals','deferred_proposals'):
                stats[key]=stats.get(key,0)+int(result.stats.get(key,0))
            issues.extend(result.issues)
            understandings.extend(result.understandings)
            if _deferred_tables(result,{table.qualified_name}):
                ledger.transition(item,'deferred');break
            existing={r.rule_id:r for r in rules}
            for rule in result.rules:
                if rule.rule_id in existing:
                    if rule_shape(rule)!=rule_shape(existing[rule.rule_id]):
                        issues.append({'code':'coverage_existing_rule_conflict','table':table.qualified_name,'status':'unresolved'})
                    continue
                rules.append(rule);existing[rule.rule_id]=rule
                execute(rule)
            after=unexplained_columns(table,validated_rules,understandings,goal,engine)
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
