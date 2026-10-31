"""Bounded rule conflicts and recoverable, source-value-free mapping material."""
from __future__ import annotations

from copy import deepcopy
import re

from pydantic import ValidationError

from lightgraph.contracts import ExtractionGoal,SourceRef,RuleSpec,canonical,digest
from lightgraph.errors import LightGraphError
from lightgraph.semantic_schema import SemanticSchema
from .models import CandidateFieldMapping,CandidateRuleMapping,PendingCandidate


def candidate_dependency(table, goal=None, context_version=''):
    return digest('candidate_mapping/1',table.model_dump(mode='json',by_alias=True),
                  (goal or ExtractionGoal()).model_dump(mode='json'),context_version)


def evidence_fingerprint(packet, documents):
    """Repeated queries/reads do not become new evidence merely by a fresh ID."""
    probes={canonical({k:p[k] for k in ('operator','aggregates') if k in p})
            for p in packet.get('probe_digest',[]) if p.get('aggregates')}
    docs={canonical({k:d.get(k) for k in ('doc_id','revision','section_id','read_state')}) for d in documents}
    return digest(sorted(probes),sorted(docs))


def proposal_shape(proposal):
    """Ignore names and citations without removing policy or scope obligations."""
    value=proposal.claim.model_dump(mode='json');value.pop('group_id',None)
    def strip(value):
        if isinstance(value,dict):return {k:strip(v) for k,v in value.items()
            if not k.endswith('_evidence') and k not in {'evidence_ref','definition_ref','scope_evidence_ref'}}
        if isinstance(value,list):return [strip(v) for v in value]
        return value
    if proposal.kind in {'column_group','entity_key'}:
        value=strip(value)
        if proposal.kind=='column_group':
            members=value.pop('members');namespaces=[m['column_id'] for m in members if m['role']=='namespace']
            keys=list(dict.fromkeys(namespaces+[m['column_id'] for m in members if m['in_identity'] and
                (m['role']=='identity_code' or m['role']=='semantic_attribute' and m['property_name']=='business_code')]))
            attributes=[(m['property_name'] or m['role'],m['column_id'],m['multi_value'] or m['role']=='alias')
                for m in members if m['role'] in {'label','alias','semantic_attribute'} and not m['exclude_from_projection']]
            excluded=sorted(m['column_id'] for m in members if m['exclude_from_projection'] or
                m['role'] in {'audit','display_order','fact_value'})
        else:
            keys=value.pop('key_columns');namespaces=value.pop('namespace_columns');attrs=value.pop('attributes')
            attributes=[(attrs['property_names'].get(column,'label' if field=='single_valued' else 'alias'),column,field=='multi_valued')
                for field in ('single_valued','multi_valued') for column in attrs[field]]
            excluded=sorted(attrs['display_only'])
            if value.get('level_policy')=='not_applicable':value.pop('level_policy')
            if value.get('normalization')=='identity':value.pop('normalization')
        return digest('column_group_entity',value,keys,namespaces,sorted(attributes),excluded)
    return digest(proposal.kind,strip(value))


def proposal_logical_id(proposal, table):
    """Identify the same object across group names and entity proposal tools."""
    from .engine import _column_id,_logical_entity_rule_id
    lookup={_column_id(table,c.name):c.name for c in table.columns};c=proposal.claim
    try:
        if proposal.kind=='column_group':
            scopes=[lookup[m.column_id] for m in c.members if m.role=='namespace']
            keys=[lookup[m.column_id] for m in c.members if m.in_identity and
                (m.role=='identity_code' or m.role=='semantic_attribute' and m.property_name=='business_code')]
            return _logical_entity_rule_id(table,'column_group_entity',c.entity_kind_hint,
                list(dict.fromkeys(scopes+keys)),scopes)
        scopes=[lookup[n] for n in getattr(c,'namespace_columns',[])]
        if proposal.kind=='entity_key':
            return _logical_entity_rule_id(table,'column_group_entity',c.entity_kind_hint,
                [lookup[n] for n in c.key_columns],scopes)
        if proposal.kind=='long_hierarchy':
            return _logical_entity_rule_id(table,'long_hierarchy',c.entity_kind_hint,
                scopes+[lookup[c.key_column]],scopes,topology={'parent':lookup[c.parent_column]})
        if proposal.kind=='wide_hierarchy':
            levels=c.levels
            return _logical_entity_rule_id(table,'wide_hierarchy',c.entity_kind_hint,
                scopes+[lookup[levels[0].key]],scopes,
                topology=[{'level':level.position,'code':lookup[level.key]} for level in levels])
        if proposal.kind=='concept_mapping':
            from lightgraph.contracts import ConceptMappingSpec
            mapping=ConceptMappingSpec(evidence_ref=c.evidence_ref,components=[part.model_copy(update={
                'column':lookup[part.column],'key_column':lookup[part.key_column] if part.key_column else None,
                'namespace_columns':[lookup[n] for n in part.namespace_columns] if part.namespace_columns is not None else None})
                for part in c.components])
            return _logical_entity_rule_id(table,'concept_mapping',c.entity_kind_hint,
                list(dict.fromkeys(scopes+[lookup[n] for n in c.key_columns])),scopes,topology=mapping.model_dump())
    except (KeyError,IndexError,ValidationError):return None
    return None


def _without_evidence(value):
    if isinstance(value,dict):return {k:_without_evidence(v) for k,v in value.items()
        if not k.endswith('_evidence')}
    if isinstance(value,list):return [_without_evidence(v) for v in value]
    return value


def merge_compatible_rules(rules):
    """Union supplemental attributes, never choose between contradictory values.

    Every input has already passed its proposal checks. The merged RuleSpec is
    revalidated here and still undergoes ordinary FD/full execution downstream.
    """
    rules=sorted(rules,key=lambda rule:canonical(rule.model_dump(mode='json')))
    first=rules[0].model_dump(mode='json')
    merged=deepcopy(first)
    def shell(value):
        value=deepcopy(value)
        for field in ('attributes','evidence','semantic_schema','goal_refs'):
            value.pop(field,None)
        value=_without_evidence(value)
        if value.get('hierarchy'):
            for level in value['hierarchy']['levels']:level.pop('aliases',None)
        return value
    for rule in rules[1:]:
        other=rule.model_dump(mode='json')
        if shell(first)!=shell(other):return None
        # Conflicting vocabulary definitions are not last-write-wins updates.
        for category in ('entity_types','properties','relations'):
            indexed={x['name']:x for x in merged['semantic_schema'][category]}
            for item in other['semantic_schema'][category]:
                if item['name'] in indexed and indexed[item['name']]!=item:return None
                indexed[item['name']]=item
            merged['semantic_schema'][category]=list(indexed.values())
        a,b=merged['attributes'],other['attributes']
        if a['excluded']!=b['excluded']:return None
        for name,column in b['single_valued'].items():
            if name in a['multi_valued'] or name in a['name_columns']:return None
            if name in a['single_valued'] and a['single_valued'][name]!=column:return None
            a['single_valued'][name]=column
        for name,column in b['multi_valued'].items():
            if name in a['single_valued']:return None
            if name in a['multi_valued'] and a['multi_valued'][name]!=column:
                if name not in {'alias','english_name'}:return None
                a['name_columns'].setdefault(name,[]).append(column)
            else:a['multi_valued'][name]=column
        for name,columns in b['name_columns'].items():
            if name in a['single_valued']:return None
            a['name_columns'].setdefault(name,[]).extend(columns)
        for name,columns in a['name_columns'].items():
            a['name_columns'][name]=sorted(set(columns)-{a['multi_valued'].get(name)})
        a['name_columns']={k:v for k,v in a['name_columns'].items() if v}
        if merged.get('hierarchy'):
            for index,level in enumerate(merged['hierarchy']['levels']):
                level['aliases']=sorted(set(level['aliases']+other['hierarchy']['levels'][index]['aliases']))
        for owner in ('identity','hierarchy'):
            if merged.get(owner):
                for field,value in merged[owner].items():
                    if field.endswith('_evidence') and isinstance(value,list):
                        merged[owner][field]=sorted(set(value+other[owner][field]))
        if merged.get('concept_mapping'):
            for index,component in enumerate(merged['concept_mapping']['components']):
                component['identity_evidence']=sorted(set(component['identity_evidence']+
                    other['concept_mapping']['components'][index]['identity_evidence']))
        for field,value in merged['evidence'].items():
            incoming=other['evidence'][field]
            if isinstance(value,list):merged['evidence'][field]=sorted(set(value+incoming))
            elif value!=incoming:return None
        merged['goal_refs']=sorted(set(merged['goal_refs']+other['goal_refs']))
    try:return RuleSpec.model_validate(merged)
    except ValidationError:return None


def _field(role, columns, allowed, *, property_name=None,multi=False,position=None):
    values=list(dict.fromkeys(c for c in columns if c in allowed))
    if not values:return None
    return CandidateFieldMapping(role=role,columns=values,property_name=property_name,multi=multi,position=position)


def rule_mapping(rule, table, *, proposal=None, goal=None):
    from .engine import _column_id,SENSITIVE
    from .goal import column_is_excluded,column_is_evidence_only
    goal=goal or ExtractionGoal()
    metadata_ids={c.name:_column_id(table,c.name) for c in table.columns}
    ids={c.name:metadata_ids[c.name] for c in table.columns if not SENSITIVE.search(c.name)
        and not column_is_excluded(goal,table,c.name) and not column_is_evidence_only(goal,table,c.name)}
    fields=[]
    def add(role,names,**kwargs):
        value=_field(role,[ids[n] for n in names if n in ids],set(ids.values()),**kwargs)
        if value:fields.append(value)
    add('key',rule.identity.key_columns);add('namespace',rule.scope.namespaces)
    for mode in ('single_valued','multi_valued'):
        for prop,column in getattr(rule.attributes,mode).items():
            if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}',prop):
                add('attribute',[column],property_name=prop,multi=mode=='multi_valued')
    for prop,columns in rule.attributes.name_columns.items():add('attribute',columns,property_name=prop,multi=True)
    excluded=_field('excluded',[metadata_ids[item.column] for item in rule.attributes.excluded
        if item.column in metadata_ids],set(metadata_ids.values()))
    if excluded:fields.append(excluded)
    if rule.hierarchy:
        if rule.hierarchy.parent_column:add('parent',[rule.hierarchy.parent_column])
        for level in rule.hierarchy.levels:
            add('hierarchy_key',[level.code],position=level.level)
            if level.label:add('hierarchy_label',[level.label],position=level.level)
            add('hierarchy_alias',level.aliases,multi=True,position=level.level)
    if rule.concept_mapping:
        for index,c in enumerate(rule.concept_mapping.components):
            add('concept_key',[c.key_column or c.column],position=index)
            add('concept_label',[c.column],position=index)
    if rule.relation:
        add('reference_source',rule.relation.source_columns);add('reference_target',rule.relation.target_columns)
    kind=proposal.kind if proposal else ('column_group' if rule.kind=='column_group_entity' else rule.kind)
    guard=guard_shape(rule)
    variant=proposal_shape(proposal) if proposal else digest(guard,[f.model_dump() for f in fields])
    reread=[]
    if rule.identity.identity_domain or rule.identity.semantic_namespace:reread.append('identity_domain')
    if rule.scope.predicate:reread.append('scope_condition')
    if rule.hierarchy and rule.hierarchy.context_qualifiers:reread.append('context_qualifiers')
    roles=[];relations=[]
    if rule.concept_mapping:
        reread.append('concept_definitions')
        from lightgraph.store.policy import RELATION_ENDPOINT_KINDS
        registered=set(RELATION_ENDPOINT_KINDS)|{r.name for r in rule.semantic_schema.relations}
        for c in rule.concept_mapping.components:
            roles.append(c.role if c.role in {'metric','product','channel','object','dimension','measure'} else 'requires_reread')
            if c.relation in registered:relations.append(c.relation)
        if 'requires_reread' in roles:reread.append('component_roles')
    return CandidateRuleMapping(variant_id=variant,rule_id=rule.rule_id,proposal_kind=kind,
        entity_kind=rule.identity.entity_kind if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}',rule.identity.entity_kind) else None,
        fields=fields,guard_fingerprint=digest(guard),identity_resolution=rule.identity.semantic_resolution,
        identity_scope=rule.identity.identity_scope,level_policy=rule.identity.level_policy,
        path_semantics=('relative_path' if rule.identity.level_policy=='relative_path' else 'legacy') if rule.kind=='wide_hierarchy' else None,
        position_semantics=rule.hierarchy.position_semantics if rule.hierarchy else None,
        allow_dag=rule.hierarchy.allow_dag if rule.hierarchy else None,
        component_roles=roles,component_relations=relations,required_reread=reread)


def proposal_mapping(proposal, packet):
    """Recover an uncompiled interpretation without persisting its free text."""
    from lightgraph.store.policy import PROPERTY_NAMES,KINDS
    allowed={c['id'] for c in packet.get('metadata_digest',{}).get('columns',[])}
    properties=set(PROPERTY_NAMES)|{p['name'] for p in packet.get('semantic_schema',{}).get('properties',[])}
    kinds=set(KINDS['business'])|{p['name'] for p in packet.get('semantic_schema',{}).get('entity_types',[])}
    fields=[];c=proposal.claim
    def add(role,columns,**kw):
        field=_field(role,columns,allowed,**kw)
        if field:fields.append(field)
    if proposal.kind=='column_group':
        for m in c.members:
            if m.in_identity:add('key',[m.column_id])
            if m.role=='namespace':add('namespace',[m.column_id])
            if m.exclude_from_projection or m.role in {'audit','display_order','fact_value'}:add('excluded',[m.column_id])
            prop=m.property_name or (m.role if m.role in {'label','alias'} else None)
            if prop in properties:add('attribute',[m.column_id],property_name=prop,multi=m.multi_value or m.role=='alias')
    elif proposal.kind=='entity_key':
        add('key',c.key_columns);add('namespace',c.namespace_columns)
        for mode in ('single_valued','multi_valued'):
            for column in getattr(c.attributes,mode):
                prop=c.attributes.property_names.get(column,'label' if mode=='single_valued' else 'alias')
                if prop in properties:add('attribute',[column],property_name=prop,multi=mode=='multi_valued')
        add('excluded',c.attributes.display_only)
    elif proposal.kind in {'wide_hierarchy','long_hierarchy'}:
        add('namespace',c.namespace_columns)
        if proposal.kind=='wide_hierarchy':
            for level in c.levels:
                add('hierarchy_key',[level.key],position=level.position)
                add('hierarchy_label',[level.label],position=level.position)
                add('hierarchy_alias',[level.alias,*level.aliases],position=level.position,multi=True)
        else:
            add('key',[c.key_column]);add('parent',[c.parent_column])
            add('attribute',[c.label_column],property_name='label')
            add('attribute',c.alias_columns,property_name='alias',multi=True)
    elif proposal.kind=='concept_mapping':
        add('key',c.key_columns);add('namespace',c.namespace_columns)
        for index,component in enumerate(c.components):
            add('concept_key',[component.key_column or component.column],position=index)
            add('concept_label',[component.column],position=index)
    elif proposal.kind=='reference':
        add('reference_source',c.source_columns);add('reference_target',c.target_columns)
    resolution=getattr(c,'identity_resolution',None)
    if resolution not in {'source_scoped','shared_definition','verified_identity_domain','verified_definition_registry'}:resolution=None
    reread=[]
    if getattr(c,'identity_domain',None):reread.append('identity_domain')
    if getattr(c,'context_qualifiers',None):reread.append('context_qualifiers')
    if proposal.kind=='concept_mapping':reread.extend(['concept_definitions','component_roles'])
    return CandidateRuleMapping(variant_id=proposal_shape(proposal),proposal_kind=proposal.kind,
        entity_kind=getattr(c,'entity_kind_hint',None) if getattr(c,'entity_kind_hint',None) in kinds else None,
        fields=fields,guard_fingerprint=digest(_without_evidence(c.model_dump(mode='json'))),
        identity_resolution=resolution,identity_scope=getattr(c,'identity_scope',None),
        path_semantics=getattr(c,'path_semantics',None),level_policy=getattr(c,'level_policy',None),
        position_semantics=getattr(c,'position_semantics',None),allow_dag=getattr(c,'allow_dag',None),
        required_reread=reread)


def guard_shape(rule):
    """Display changes cannot erase scope, identity, conditions or exclusions."""
    value=rule.model_dump(mode='json')
    guard=_without_evidence({k:value[k] for k in ('kind','source','scope','identity',
        'normalization','operators','required_capabilities','hierarchy','relation','concept_mapping')
        if k in value}) | {'excluded':value['attributes']['excluded']}
    if guard.get('hierarchy'):
        for level in guard['hierarchy']['levels']:
            level.pop('label',None);level.pop('aliases',None)
    return guard


def conflict_record(variants, table, goal, packet, documents):
    mappings=[rule_mapping(rule,table,proposal=proposal,goal=goal) for proposal,_,rule in variants]
    columns=sorted({c for m in mappings for f in m.fields for c in f.columns})
    docs=sorted({d['doc_id']+'@'+d['revision']+':'+d['section_id'] for d in documents
        if all(d.get(k) for k in ('doc_id','revision','section_id'))})
    signatures={m.guard_fingerprint for m in mappings}
    labels={canonical([f.model_dump() for f in m.fields if f.property_name=='label' or f.role=='hierarchy_label']) for m in mappings}
    differences=[]
    if len(signatures)>1:differences.append('identity_policy')
    if len(labels)>1:differences.append('label_mapping')
    if not differences:differences.append('attribute_mapping')
    source=SourceRef(source_id=table.source_id,schema=table.schema_name,table=table.name)
    candidate=PendingCandidate(candidate_id='candidate_'+digest(source.model_dump(),variants[0][2].rule_id,
        sorted(m.variant_id for m in mappings))[:24],source=source,proposal_kind=variants[0][0].kind,
        failure_code='conflicting_rule_proposals',column_refs=columns,document_refs=docs[:128],
        rule_refs=[variants[0][2].rule_id],review_stage='entity_definition',mappings=mappings,
        differences=differences,dependency_fingerprint=packet.get('iteration_context',{}).get('candidate_dependency'))
    return {'candidate':candidate,'variants':variants}


def conflict_fingerprint(record, packet, documents):
    return digest(sorted(m.variant_id for m in record['candidate'].mappings),evidence_fingerprint(packet,documents))


def mark_conflict_repaired(result, candidate_id, rule_id):
    if candidate_id not in result.resolved_candidate_ids:
        result.resolved_candidate_ids.append(candidate_id)
        result.stats['rule_conflict_revisions_compiled']=result.stats.get('rule_conflict_revisions_compiled',0)+1
    result.candidate_replacement_rules[candidate_id]=[rule_id]
    for issue in result.issues:
        if issue.get('code')=='conflicting_rule_proposals' and issue.get('rule_id')==rule_id:
            issue['status']='resolved'


def prepare_candidate_context(packet, table, goal, corpus):
    """Retire stale interpretations before they reach the next Agent prompt."""
    from lightgraph.context import context_dependency_fingerprint
    context=packet.setdefault('iteration_context',{})
    dependency=candidate_dependency(table,goal,context_dependency_fingerprint(corpus,[table],goal))
    context['candidate_dependency']=dependency
    from .engine import _column_id
    columns={_column_id(table,c.name) for c in table.columns}
    kept=[]
    for item in context.get('pending_candidates',[]):
        source=item.get('source',{})
        if (source.get('source_id'),source.get('schema','public'),source.get('table'))!=(table.source_id,table.schema_name,table.name):
            continue
        if item.get('dependency_fingerprint') not in (None,dependency):continue
        if item.get('mappings') and item.get('dependency_fingerprint') is None:continue
        referenced=set(item.get('column_refs',[]))|{column for mapping in item.get('mappings',[])
            for field in mapping.get('fields',[]) for column in field.get('columns',[])}
        if not referenced<=columns:continue
        kept.append(item)
    context['pending_candidates']=kept[:16]


def retain_unchanged_conflicts(batch, packet, documents, result, table):
    """A restart must not solicit the same judgments for the same material."""
    proposals=[p for p in batch.proposals if p.kind!='exclusion']
    blocked=set()
    for raw in packet.get('iteration_context',{}).get('pending_candidates',[]):
        if raw.get('failure_code')!='conflicting_rule_proposals':continue
        if raw.get('dependency_fingerprint')!=packet.get('iteration_context',{}).get('candidate_dependency'):continue
        try:candidate=PendingCandidate.model_validate(raw)
        except ValidationError:continue
        previous={m.variant_id for m in candidate.mappings}
        matching=[p for p in proposals if proposal_logical_id(p,table) in candidate.rule_refs]
        if not matching:continue
        result.pending_candidates.append(candidate)
        shapes={proposal_shape(p) for p in matching}
        fingerprint=digest(sorted(previous|shapes),evidence_fingerprint(packet,documents))
        if fingerprint in candidate.attempted_fingerprints or len(candidate.attempted_fingerprints)>=8:
            blocked.update(shapes)
            result.issues.append({'table':table.qualified_name,'code':'rule_conflict_unchanged',
                'rule_id':candidate.rule_refs[0],'status':'unresolved'})
        else:
            candidate.attempted_fingerprints.append(fingerprint)
            packet.setdefault('iteration_context',{}).setdefault('rule_conflict_requests',[]).append({
                'candidate':candidate.model_dump(mode='json',by_alias=True),'restored':True,
                'alternatives':[]})
            result.issues.append({'table':table.qualified_name,'code':'conflicting_rule_proposals',
                'rule_id':candidate.rule_refs[0],'status':'deferred'})
    if not blocked:return batch
    return batch.model_copy(update={'proposals':[p for p in batch.proposals
        if p.kind=='exclusion' or proposal_shape(p) not in blocked]})


def propose_conflict_revisions(records, *, model,packet,tool_factory,ask,observations,result,table,documents):
    """One small conflict session, with all competing guards kept in context."""
    prior={p['candidate_id']:p for p in packet.get('iteration_context',{}).get('pending_candidates',[])}
    eligible=[]
    for record in records:
        candidate=record['candidate'];old=prior.get(candidate.candidate_id,{})
        if old.get('dependency_fingerprint')==candidate.dependency_fingerprint:
            candidate.attempted_fingerprints=list(old.get('attempted_fingerprints',[]))[:8]
        fingerprint=conflict_fingerprint(record,packet,documents)
        if fingerprint in candidate.attempted_fingerprints or len(candidate.attempted_fingerprints)>=8:
            result.issues.append({'table':table.qualified_name,'code':'rule_conflict_unchanged','status':'unresolved'})
            continue
        eligible.append((record,fingerprint))
    if not eligible or tool_factory is None or not callable(getattr(model,'explore',None)):
        return [],documents
    if len(eligible)>1:
        result.issues.append({'table':table.qualified_name,'code':'rule_conflict_revision_deferred',
            'count':len(eligible)-1,'status':'deferred'})
    eligible=sorted(eligible,key=lambda item:item[0]['candidate'].candidate_id)[:1]
    requests=[{'candidate':record['candidate'].model_dump(mode='json',by_alias=True),
        'alternatives':[proposal.model_dump(mode='json',exclude={'rationale','requested_probes'})
                        for proposal,_,_ in record['variants']]} for record,_ in eligible]
    repair_packet={**packet,'task':'T5_revision','iteration_context':{
        **packet.get('iteration_context',{}),'rule_conflict_requests':requests,
        'pending_candidates':[]},
        'subtask':'Resolve only the listed competing mappings. Read the relevant authorized metadata, '
        'documents or bounded probes, and submit one complete proposal per logical object. Preserve all '
        'exclusions, identity/scope/condition obligations and counterevidence. A different group name is '
        'not progress. Selecting an unchanged alternative requires new evidence. If evidence cannot '
        'resolve the conflict, abstain; independent good rules are already retained.'}
    before_evidence=evidence_fingerprint(packet,documents)
    tools=tool_factory();alternatives=[]
    try:
        for record,fingerprint in eligible:record['candidate'].attempted_fingerprints.append(fingerprint)
        result.stats['rule_conflict_revision_attempts']=result.stats.get('rule_conflict_revision_attempts',0)+1
        batch=ask(model,repair_packet,tools,observations)
        refreshed=tools.context_documents()
        by_ref={f"{d['doc_id']}@{d['revision']}:{d['section_id']}":d for d in documents+refreshed}
        documents=list(by_ref.values())
        after_evidence=evidence_fingerprint(repair_packet,documents)
        for record,_ in eligible:
            fingerprint=conflict_fingerprint(record,repair_packet,documents)
            if fingerprint not in record['candidate'].attempted_fingerprints:
                record['candidate'].attempted_fingerprints=record['candidate'].attempted_fingerprints[:7]+[fingerprint]
        if batch.abstain:return [],documents
        used=set()
        from .proposal_session import _compatible_replacement
        for proposal in batch.proposals:
            if proposal.kind=='exclusion':continue
            matches=[]
            for record,_ in eligible:
                for original,_,_ in record['variants']:
                    if original.kind!=proposal.kind:continue
                    old=original.model_copy(deep=True)
                    if hasattr(old.claim,'group_id'):old.claim.group_id=proposal.claim.group_id
                    if _compatible_replacement(old,proposal):matches.append(record);break
            if len(matches)!=1:
                result.issues.append({'table':table.qualified_name,'code':'rule_conflict_constraints_changed','status':'unresolved'})
                continue
            record=matches[0];candidate=record['candidate']
            if candidate.candidate_id in used:continue
            if proposal_shape(proposal) in {m.variant_id for m in candidate.mappings} and before_evidence==after_evidence:
                result.issues.append({'table':table.qualified_name,'code':'rule_conflict_no_progress','status':'unresolved'})
                continue
            used.add(candidate.candidate_id)
            fingerprint=digest(sorted({m.variant_id for m in candidate.mappings}|{proposal_shape(proposal)}),after_evidence)
            if fingerprint not in candidate.attempted_fingerprints:
                candidate.attempted_fingerprints=candidate.attempted_fingerprints[:7]+[fingerprint]
            alternatives.append((record,proposal,batch.semantic_schema,repair_packet))
        return alternatives,documents
    except LightGraphError as exc:
        result.issues.append({'table':table.qualified_name,'code':exc.code,'status':'deferred'})
        return [],documents
    finally:tools.close()


def execution_candidate(rule, codes, *, table, goal=None, documents=(),context_version=''):
    """Return safe retry material for known validation failures; never raise."""
    from typing import get_args
    try:
        allowed=set(get_args(PendingCandidate.model_fields['failure_details'].annotation.__args__[0]))
        details=sorted(set(codes)&allowed)
        if not details:return None
        mapping=rule_mapping(rule,table,goal=goal)
        source=rule.source
        return PendingCandidate(candidate_id='candidate_'+digest(source.model_dump(),rule.rule_id,details)[:24],
            source=source,proposal_kind=mapping.proposal_kind,failure_code='rule_execution_failed',
            column_refs=sorted({c for f in mapping.fields for c in f.columns}),
            document_refs=sorted(set(rule.dependencies.documents))[:128],rule_refs=[rule.rule_id],
            review_stage='reference' if rule.kind=='reference' else 'entity_definition',mappings=[mapping],
            failure_details=details,dependency_fingerprint=candidate_dependency(table,goal,context_version))
    except (ValidationError,ValueError,TypeError,AttributeError):return None
