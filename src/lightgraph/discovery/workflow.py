"""Agent discovery produces rules; complete rule execution alone may publish them.

Only schema references, roles, counts and fixed issue codes escape this module.
The Agent and query tools own ephemeral observations and never persist transcripts.
"""
from __future__ import annotations

from lightgraph.contracts import (ExtractionGoal, ExcludedColumn, RuleSpec, IdentitySpec,
    ScopeSpec, RelationSpec, Dependencies, Coverage, RuleEvidence, digest)
from lightgraph.documents import DocumentCorpus
from lightgraph.errors import LightGraphError, BudgetExceeded
from .budget import BudgetAllocator, Cost
from .goal import source_in_goal, column_is_excluded, column_is_evidence_only
from .models import DiscoveryResult, Hypothesis, ProposalBatch


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


def _ask(model, packet, tools):
    return model.explore(packet, tools,
        max_steps=getattr(model.config,'max_discovery_steps',8),
        max_tool_calls=getattr(model.config,'max_tool_calls',16))


def _issue(result, table, code, *, count=None):
    entry={'table':table.qualified_name,'code':code,
           'status':'deferred' if any(token in code for token in ('budget','limit','exhausted')) else 'unresolved'}
    if count is not None: entry['count']=count
    result.issues.append(entry)


def _compile(batch, table, goal, docs, result):
    from .engine import _compile_model, SENSITIVE
    excluded=[ExcludedColumn(column=c.name,reason='sensitive_or_excluded')
        for c in table.columns if SENSITIVE.search(c.name) or column_is_excluded(goal,table,c.name)]
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
            if getattr(p.claim,'entity_kind_hint',None) not in goal.entity_kinds:
                raise LightGraphError('outside_extraction_goal')
            local=ProposalBatch(task=batch.task,proposals=exclusions+[p])
            candidates=_compile_model(local,table,goal,docs,excluded)
            for rule in candidates:
                if rule.hierarchy and 'parent_of' not in goal.relation_kinds:
                    raise LightGraphError('outside_relation_goal')
                # The same independent sharing declaration used by deterministic
                # discovery applies to Agent-defined columns as well.
                from .engine import _document_evidence
                sharing=_document_evidence(docs,table,
                    r'同一(?:完整|业务)?定义.{0,24}(?:多个|不同)租户.{0,12}(?:引用|共享)|same definition.{0,32}across tenants')
                if rule.kind=='column_group_entity' and sharing and rule.scope.namespaces and rule.attributes.single_valued.get('definition'):
                    rule.identity.semantic_resolution='verified_definition_registry'
                    rule.identity.semantic_namespace='declared_definition:'+sharing[0].split('@',1)[0]+':'+rule.identity.entity_kind
                    rule.evidence.semantic_basis.extend(sharing)
                rules.append(rule)
        except LightGraphError as exc:
            _issue(result,table,exc.code)
    unique={}
    conflicts=set()
    for rule in rules:
        previous=unique.get(rule.rule_id)
        if previous is not None and previous!=rule:
            conflicts.add(rule.rule_id);_issue(result,table,'conflicting_rule_proposals')
        unique[rule.rule_id]=rule
    return [rule for rid,rule in unique.items() if rid not in conflicts]


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


def discover_with_agent(adapter, goal=None, documents=None, model=None, budget=None):
    from .engine import _source, _packet, _understanding, _table_hash, _column_id
    from .tools import ToolExecutor
    goal=goal or ExtractionGoal()
    budget=budget or model.budget
    corpus=documents if isinstance(documents,DocumentCorpus) else DocumentCorpus(documents or [])
    result=DiscoveryResult()
    tables=_catalog(adapter,goal)
    originals={t.qualified_name:t for t in adapter.describe()}
    if len(tables)>64:
        result.issues.append({'code':'catalog_table_limit','status':'partial'})
        tables=tables[:64]
    for table in tables:
        original=originals[table.qualified_name]
        docs=corpus.context_for(_source(table),table.column_names)
        understanding=_understanding(table,[],{},docs)
        understanding.dependency_hash=_table_hash(original)
        result.understandings.append(understanding)
        reservation='agent_validation_'+digest(table.qualified_name)[:16]
        tools=None
        try:
            # Exploration cannot consume the minimum reserved validation work.
            budget.reserve(reservation,Cost(db_cu=2))
            if len(table.columns)>256: raise LightGraphError('catalog_column_limit')
            packet=_packet(table,goal,[],docs,'T0_discovery',understanding)
            packet['catalog']=[t.model_dump(mode='json',by_alias=True) for t in tables]
            tools=ToolExecutor(adapter,goal,budget,documents=corpus,
                allow_source_samples=getattr(model.config,'allow_source_samples',False),allowed_tables=tables)
            batch=_ask(model,packet,tools)
            budget.release(reservation)
            if batch.abstain: _issue(result,table,'model_abstained')
            rules=_compile(batch,original,goal,docs,result)
            by_id={_column_id(table,c.name):c.name for c in table.columns}
            roles={by_id[m.column_id]:m.role for p in batch.proposals if p.kind=='column_group'
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
                        allow_source_samples=getattr(model.config,'allow_source_samples',False),allowed_tables=tables)
                    fixed=_compile(_ask(model,revision,tools),original,goal,docs,result)
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
            budget.release(reservation)
    result.stats={'tables_considered':len(tables),'rules_proposed':len(result.rules),
        'model_calls':model.usage['calls'],'discovery_mode':'agent_tools',
        'semantic_validation':'requires_rule_engine','coverage':'discovery_only'}
    result.budget=budget.snapshot()
    return result


def discover_references_with_agent(adapter, validated_rules, documents=None, model=None,
                                   budget=None, goal=None, max_candidates=32):
    """Resolve differently named local endpoint columns to verified entity keys."""
    from .engine import _source, _packet, _understanding, _table_hash, _column_id, _document_evidence
    from .tools import ToolExecutor
    goal=goal or ExtractionGoal();budget=budget or model.budget
    corpus=documents if isinstance(documents,DocumentCorpus) else DocumentCorpus(documents or [])
    tables=_catalog(adapter,goal);originals={t.qualified_name:t for t in adapter.describe()}
    approved={r.rule_id:r for r in validated_rules if r.status in ('validated','canary_checked','published')
              and r.validation_ref and r.kind=='column_group_entity' and source_in_goal(goal,r.source)}
    result=DiscoveryResult()
    if len(approved)<2:return result
    if len(tables)>64 or len(approved)>64:
        result.issues.append({'code':'reference_catalog_limit','status':'partial'});return result
    count=0
    for table in tables:
        tools=None
        docs=corpus.context_for(_source(table),table.column_names)
        try:
            if count>=max_candidates:raise LightGraphError('reference_candidate_limit')
            packet=_packet(table,goal,[],docs,'T2_cross_table',_understanding(table,[],{},docs))
            packet['catalog']=[t.model_dump(mode='json',by_alias=True) for t in tables]
            packet['authorized_entities']=list(approved)
            packet['entity_bindings']=[{'rule_id':r.rule_id,'source':r.source.model_dump(by_alias=True),
                'entity_kind':r.identity.entity_kind,'key_columns':r.identity.key_columns} for r in approved.values()]
            tools=ToolExecutor(adapter,goal,budget,documents=corpus,
                allow_source_samples=getattr(model.config,'allow_source_samples',False),allowed_tables=tables)
            batch=_ask(model,packet,tools)
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
                        matches=[r for r in approved.values() if r.source==_source(table) and r.identity.key_columns==local]
                        if len(matches)!=1:raise LightGraphError('reference_source_binding_ambiguous')
                        source=matches[0]
                    target=approved[claim.target_entity]
                    if claim.condition_ref or claim.polymorphic_by:raise LightGraphError('reference_condition_requires_registered_selector')
                    if claim.relation_kind not in goal.relation_kinds:raise LightGraphError('outside_relation_goal')
                    for binding,columns in ((source,local),(target,remote)):
                        destination=originals.get(binding.source.schema_name+'.'+binding.source.table)
                        if destination is None or binding.source.source_id!=table.source_id:raise LightGraphError('unavailable_reference_target')
                        if len(columns)!=len(binding.identity.key_columns):raise LightGraphError('reference_arity_mismatch')
                        budget.admit(Cost(db_cu=1))
                        try:check=adapter.check_reference(originals[table.qualified_name],columns,destination,binding.identity.key_columns)
                        finally:budget.charge(Cost(db_cu=1))
                        if any(check.get(k,0) for k in ('unmatched','null_refs','ambiguous')):raise LightGraphError('reference_missing')
                    # Overlap alone never promotes technical evidence to business semantics.
                    evidence=_document_evidence(docs,table,r'引用|关联|使用|映射|reference|maps? to')
                    if claim.relation_kind=='uses_measure':
                        evidence=_document_evidence(docs,table,r'指标.{0,20}(使用|依赖).{0,20}度量')
                    if claim.relation_kind=='member_of':
                        evidence=_document_evidence(docs,table,r'成员.{0,20}(属于|归属).{0,20}维度')
                    if not evidence:raise LightGraphError('technical_overlap_semantics_unresolved')
                    result.rules.append(RuleSpec(rule_id='rule_'+digest(table.qualified_name,source.rule_id,target.rule_id,local,remote)[:24],
                        kind='reference',goal_refs=[goal.goal_id],source=_source(table),
                        scope=ScopeSpec(namespaces=[local[source.identity.key_columns.index(c)]
                            for c in source.scope.namespaces if c in source.identity.key_columns]),
                        identity=IdentitySpec(key_columns=local,entity_kind=source.identity.entity_kind),
                        relation=RelationSpec(source_rule_id=source.rule_id,target_rule_id=target.rule_id,
                            source_columns=local,target_columns=remote,relation=claim.relation_kind),
                        dependencies=Dependencies(schema=_table_hash(originals[table.qualified_name]),
                            documents=sorted({d['doc_id']+'@'+d['revision'] for d in docs})),
                        evidence=RuleEvidence(semantic_basis=evidence),coverage=Coverage(scope='full',consistent=False)))
                except BudgetExceeded:raise
                except LightGraphError as exc:_issue(result,table,exc.code)
        except LightGraphError as exc:
            _issue(result,table,exc.code)
            if isinstance(exc,BudgetExceeded):break
        finally:
            if tools is not None:tools.close()
    result.stats={'reference_candidates':count,'rules_proposed':len(result.rules),'endpoint_rules':len(approved)}
    result.budget=budget.snapshot()
    return result
