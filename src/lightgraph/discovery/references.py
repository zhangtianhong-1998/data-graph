"""Bounded JSON and polymorphic reference discovery over validated entity registries.

Source-side DISTINCT reduces the physical facts first. A JSON array is expanded
only in memory; its elements become graph references, never a persisted evidence
list. Unknown discriminator semantics do not become executable selectors.
"""
from __future__ import annotations
import json
import math
import re
from lightgraph.contracts import (RuleSpec,ScopeSpec,IdentitySpec,RelationSpec,Dependencies,
    Coverage,digest,canonical)
from lightgraph.errors import LightGraphError,BudgetExceeded
from .budget import Cost
from .models import DiscoveryResult
from .semantics import declared_evidence, rule_evidence

DISCRIMINATOR=re.compile(r'(?:^|_)(?:source_type|ref_type|ref_kind|entity_type|target_type|business_type)$|引用字段来源|来源类型|引用类型',re.I)
REF_VALUE=re.compile(r'(?:^|_)(?:ref_code|source_field|ref_value|target_code|target_id|allowed_codes|value_range)$|引用字段$|取值范围$',re.I)
ARRAY_SUFFIX=re.compile(r'(?:_codes|_ids|_keys|_code_list|_id_list|_json|_array|取值范围)$',re.I)

def _plain_base(name):
    name=re.sub(r'^(allowed|valid|selected|supported)_','',name.lower())
    return re.sub(r'(?:_codes?|_ids?|_keys?|_code_list|_id_list|_json|_array)$','',name)

def expand_scalar_array(value,max_elements=256):
    if not isinstance(value,str): raise LightGraphError('json_reference_requires_text')
    try:values=json.loads(value)
    except (ValueError,TypeError,RecursionError):raise LightGraphError('invalid_json_reference') from None
    if not isinstance(values,list) or len(values)>max_elements:
        raise LightGraphError('invalid_json_reference_array')
    if any(type(v) not in (str,int,float,bool) or isinstance(v,str) and len(v)>4096 or isinstance(v,float) and not math.isfinite(v) for v in values):
        raise LightGraphError('non_scalar_json_reference')
    # Type and leading zeros remain unchanged; JSON "01" is not integer 1.
    return list({canonical(v):v for v in values}.values())

def _read(adapter,table,columns,budget,*,predicate=None,limit=20000):
    budget.admit(Cost(db_cu=1,memory_bytes=1024*1024))
    rows=[];size=0
    try:
        for batch in adapter.project(table,list(dict.fromkeys(columns)),predicate=predicate,distinct=True,limit=limit):
            try:
                for row in batch.rows():
                    record=dict(zip(batch.columns,row));size+=len(canonical(record).encode())
                    if size>1024*1024:raise LightGraphError('reference_probe_memory_limit')
                    rows.append(record)
            finally:batch.release()
        return rows
    finally:budget.charge(Cost(db_cu=1,memory_bytes=min(size,1024*1024)))

def discover_complex_references(adapter,approved,corpus,budget,max_candidates=16):
    from .engine import _source,_table_hash
    result=DiscoveryResult();catalog={(t.source_id,t.schema_name,t.name):t for t in adapter.describe()}
    approved=[r for r in approved if r.kind=='column_group_entity' and r.validation_ref and r.status in ('validated','canary_checked','published')]
    count=0
    for table in catalog.values():
        if count>=max_candidates:break
        docs=corpus.context_for(_source(table),table.column_names)
        observed_selectors=set()
        discriminators=[c for c in table.column_names if DISCRIMINATOR.search(c)]
        array_candidates=[c for c in table.column_names if ARRAY_SUFFIX.search(c)]
        ref_values=[c for c in table.column_names if REF_VALUE.search(c)]
        own=[r for r in approved if r.source.source_id==table.source_id and set(r.identity.key_columns)<=set(table.column_names)]
        # Never build arrays of observation IDs into an entity graph unless their
        # owner and target have already passed the entity validator.
        for owner in own:
            for target in approved:
                if target.rule_id==owner.rule_id or target.source.source_id!=table.source_id:continue
                if len(target.identity.key_columns)<1:continue
                target_keys=target.identity.key_columns
                target_code=target_keys[-1]
                target_namespace=target_keys[:-1]
                if not set(target_namespace)<=set(table.column_names):continue
                choices=[c for c in array_candidates if _plain_base(c)==_plain_base(target_code)]
                if discriminators:choices+=ref_values
                choices=list(dict.fromkeys(choices))
                for reference_column in choices:
                    if count>=max_candidates:break
                    if reference_column in owner.identity.key_columns:continue
                    count+=1;rows=[];target_rows=[]
                    try:
                        # Probe the minimum columns needed for a joint reference;
                        # all non-target facts stay in the source database.
                        columns=owner.identity.key_columns+target_namespace+[reference_column]+discriminators
                        rows=_read(adapter,table,columns,budget)
                        if not rows:continue
                        target_table=catalog[(target.source.source_id,target.source.schema_name,target.source.table)]
                        target_rows=_read(adapter,target_table,target_keys,budget)
                        known={canonical([r[c] for c in target_keys]) for r in target_rows}
                        partitions=[(None,None,rows)]
                        if discriminators:
                            if len(discriminators)>1:
                                result.issues.append({'table':table.qualified_name,'code':'discriminator_ambiguous','status':'unresolved'});continue
                            disc=discriminators[0]
                            values={canonical(r[disc]):r[disc] for r in rows}
                            if len(values)>64:
                                result.issues.append({'table':table.qualified_name,'code':'discriminator_cardinality_limit','status':'unresolved'});continue
                            partitions=[(disc,value,[r for r in rows if canonical(r[disc])==encoded]) for encoded,value in values.items()]
                            observed_selectors.update(canonical({disc:value}) for value in values.values())
                        for disc,value,partition in partitions:
                            predicate={disc:value} if disc else None
                            evidence=[];relation='verified_reference'
                            # Exact declaration includes selector, endpoint
                            # columns and relation. A mention of JSON or a type
                            # name no longer authorizes this executable mapping.
                            for relation_kind in ('uses_measure','member_of','verified_reference'):
                                matches=declared_evidence(docs,table,relation_kind,
                                    owner.identity.key_columns,target_namespace+[reference_column],
                                    conditions=predicate)
                                if matches:
                                    evidence=matches;relation=relation_kind;break
                            if not evidence:
                                result.issues.append({'table':table.qualified_name,'code':'array_reference_semantics_unresolved','status':'unresolved'});continue
                            shapes={isinstance(r[reference_column],str) and r[reference_column].lstrip().startswith('[') for r in partition}
                            if len(shapes)>1:
                                result.issues.append({'table':table.qualified_name,'code':'mixed_scalar_array_reference','status':'unresolved'});continue
                            array=True in shapes
                            unmatched=0;elements=0
                            for row in partition:
                                values=expand_scalar_array(row[reference_column]) if array else [row[reference_column]]
                                for item in values:
                                    elements+=1
                                    key=[row[c] for c in target_namespace]+[item]
                                    if any(v is None for v in key) or canonical(key) not in known:unmatched+=1
                            if unmatched:
                                result.issues.append({'table':table.qualified_name,'code':'reference_missing','count':unmatched,'status':'unresolved'});continue
                            if not elements:continue
                            rid='rule_'+digest(table.source_id,table.qualified_name,owner.rule_id,target.rule_id,reference_column,predicate)[:24]
                            result.rules.append(RuleSpec(rule_id=rid,kind='reference',source=_source(table),
                                scope=ScopeSpec(namespaces=owner.scope.namespaces,predicate=predicate),
                                identity=IdentitySpec(key_columns=owner.identity.key_columns,entity_kind=owner.identity.entity_kind),
                                relation=RelationSpec(source_rule_id=owner.rule_id,target_rule_id=target.rule_id,
                                    source_columns=owner.identity.key_columns,target_columns=target_namespace+[reference_column],
                                    target_json_array_columns=[reference_column] if array else [],relation=relation),
                                dependencies=Dependencies(schema=_table_hash(table),documents=sorted({d['doc_id']+'@'+d['revision'] for d in docs})),
                                evidence=rule_evidence(evidence,probe_refs=['complete_distinct_json_reference' if array else 'complete_distinct_partition_reference']),
                                coverage=Coverage(scope='partition' if disc else 'full',consistent=False)))
                    except LightGraphError as exc:
                        result.issues.append({'table':table.qualified_name,'code':exc.code,'status':'deferred' if isinstance(exc,BudgetExceeded) else 'unresolved'})
                    finally:
                        rows.clear();target_rows.clear()
        if discriminators:
            # A partition manifest cannot persist the returned enum domain. Count
            # coverage here; execute each accepted selector and re-query next run.
            accepted_selectors={canonical(r.scope.predicate) for r in result.rules if r.source.table==table.name and r.scope.predicate}
            unknown_count=len(observed_selectors-accepted_selectors)
            if unknown_count or not observed_selectors:
                result.issues.append({'table':table.qualified_name,'code':'discriminator_partitions_unresolved','count':unknown_count,'status':'unresolved'})
    result.stats={'complex_reference_candidates':count,'complex_rules_proposed':len(result.rules)}
    result.budget=budget.snapshot();return result
