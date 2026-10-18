"""Check only registered discriminator domains before reusing a discovered rule."""
from __future__ import annotations
from collections import defaultdict
from pydantic import Field
from lightgraph.contracts import StrictModel, canonical
from lightgraph.errors import LightGraphError, BudgetExceeded
from lightgraph.documents import DocumentCorpus
from .budget import BudgetAllocator
from .references import DISCRIMINATOR, _read

class CachedPatternReview(StrictModel):
    issues: list[dict[str,str|int]] = Field(default_factory=list)
    needs_review: list[str] = Field(default_factory=list)
    stats: dict[str,int] = Field(default_factory=dict)


def _equalities(predicate):
    if not isinstance(predicate,dict):return []
    if set(predicate)=={'and'}:
        return [x for p in predicate['and'] for x in _equalities(p)]
    if 'column' in predicate:
        if predicate.get('op','eq')=='eq':return [(predicate['column'],predicate.get('value'))]
        return []
    result=[]
    for column,value in predicate.items():
        if isinstance(value,dict):
            if value.get('op','eq')=='eq':result.append((column,value.get('value')))
        elif type(value) in (str,int,float,bool,type(None)):result.append((column,value))
    return result


def validate_cached_patterns(adapter,rules,documents=None,budget=None) -> CachedPatternReview:
    """A zero-model reuse path still checks new discriminator values.

    Only selector columns already used by accepted reference rules are inspected.
    The returned domain stays in memory; reports contain a count and table/rule
    references, never the new raw value. New domains are scheduled for targeted
    review by the orchestrator, not silently interpreted by this function.
    """
    budget=budget or BudgetAllocator()
    corpus=documents if isinstance(documents,DocumentCorpus) else DocumentCorpus(documents or [])
    versions={d.document_id+'@'+d.revision for d in corpus.documents}
    result=CachedPatternReview();groups=defaultdict(lambda:{'values':set(),'rules':set()})
    catalog={(t.source_id,t.schema_name,t.name):t for t in adapter.describe()}
    for rule in rules:
        if rule.status not in ('validated','canary_checked','published') or not rule.validation_ref:continue
        if rule.dependencies.documents and not set(rule.dependencies.documents)<=versions:
            result.issues.append({'table':rule.source.table,'rule_id':rule.rule_id,'code':'document_dependency_stale'})
            result.needs_review.append(rule.rule_id)
        if rule.kind!='reference':continue
        key=(rule.source.source_id,rule.source.schema_name,rule.source.table)
        if key not in catalog:continue
        for column,value in _equalities(rule.scope.predicate):
            if not DISCRIMINATOR.search(column):continue
            groups[key+(column,)]['values'].add(canonical(value))
            groups[key+(column,)]['rules'].add(rule.rule_id)
    checked=0
    for (source_id,schema,table_name,column),registered in groups.items():
        table=catalog[(source_id,schema,table_name)];rows=[]
        try:
            # 65 values provide a hard signal when the low-cardinality contract
            # no longer holds. This is a complete DISTINCT read, not a first-page
            # sample; a 66th row is rejected by the source adapter's lookahead.
            rows=_read(adapter,table,[column],budget,limit=65)
            observed={canonical(row[column]) for row in rows}
            checked+=1
            unknown=observed-registered['values']
            if len(observed)>64:
                result.issues.append({'table':table.qualified_name,'code':'discriminator_cardinality_drift','count':len(observed)})
            elif unknown:
                result.issues.append({'table':table.qualified_name,'code':'new_discriminator_partition','count':len(unknown)})
            if len(observed)>64 or unknown:result.needs_review.extend(registered['rules'])
        except LightGraphError as exc:
            result.issues.append({'table':table.qualified_name,'code':exc.code})
            result.needs_review.extend(registered['rules'])
        finally:rows.clear()
    result.needs_review=sorted(set(result.needs_review))
    result.stats={'registered_discriminator_columns':len(groups),'checked_discriminator_columns':checked}
    return result
