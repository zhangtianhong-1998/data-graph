"""Goal-scoped structural proposal workflow. All generated rules require revalidation."""
from __future__ import annotations
from collections import defaultdict
import re
from typing import Iterable
from lightgraph.contracts import (TableSpec, SourceRef, ExtractionGoal, RuleSpec, ScopeSpec,
    IdentitySpec, AttributeSpec, ExcludedColumn, HierarchySpec, LevelSpec, RelationSpec,
    Dependencies, RuleEvidence, Coverage, digest, canonical)
from lightgraph.errors import LightGraphError, BudgetExceeded
from lightgraph.documents import DocumentCorpus
from .budget import BudgetAllocator, Cost
from .models import (DiscoveryResult, Hypothesis, DataUnderstanding, ColumnUnderstanding,
                     ProposalBatch, ColumnGroupProposal, EntityKeyProposal, HierarchyProposal)
from .semantics import declared_evidence, rule_evidence
from lightgraph.semantic_schema import SemanticSchema, EntityTypeDefinition

SENSITIVE = re.compile(r'password|secret|token|private|phone|email|identity_card|id_card|身份证|手机号|密码|密钥',re.I)
AUDIT = re.compile(r'^(created|updated|deleted)_(at|by|time)$|^create_time$|^update_time$|^审计|^更新时间$',re.I)
SORT = re.compile(r'(?:^|_)(?:sort|rank|order)(?:_column|_no|_index)?$|排序|序号$',re.I)
SCOPE = re.compile(r'^(?:tenant|workspace|account)_id$|^租户(?:编码|编号|ID)?$',re.I)
LEVEL = re.compile(r'(^|_)(?:l|level_?)([1-9][0-9]?)(?=_|$)',re.I)
NAME = re.compile(r'^(.*?)(?:_cn_name|_zh_name|_name|_label|中文名|名称|名字)$',re.I)
ALIAS = re.compile(r'^(.*?)(?:_en_name|_alias|_english_name|英文名|别名)$',re.I)
CODE = re.compile(r'^(.*?)(?:_code|_id|_key|编码|编号|代码)$',re.I)
SEMANTIC = {'definition':'definition','description':'description','unit':'unit','formula':'formula',
            '定义':'definition','单位':'unit','公式':'formula'}

def _source(table): return SourceRef(source_id=table.source_id,schema=table.schema_name,table=table.name)
def _table_hash(table): return digest(table.model_dump(mode='json',by_alias=True))
def _column_id(table,name): return 'c_'+digest(table.source_id,table.qualified_name,name)[:16]
def _comment_quality(name,comment):
    if not comment or not comment.strip(): return 'empty'
    if comment.strip().casefold()==name.strip().casefold(): return 'echo'
    if comment.strip() in ('编码','名称','描述','编号','code','name','id'): return 'generic'
    return 'informative'
def _kind(prefix,table,goal,documents=()):
    from lightgraph.store.policy import KINDS
    allowed=set(goal.entity_kinds) if goal.entity_kinds else KINDS['business']
    text=' '.join([prefix,table.comment]+[c.comment for c in table.columns if c.name.startswith(prefix)])
    checks=[('Metric',r'metric|指标'),('Measure',r'measure|度量'),('DimensionMember',r'dimension|(?:^|_)dim(?:_|$)|维度成员|字典'),
            ('ReportItem',r'report.?item|报表项目')]
    for kind,pattern in checks:
        if kind in allowed and re.search(pattern,text,re.I): return kind
    for document in documents:
        if not _definition_document(document, table):continue
        declaration=re.search(re.escape(table.name)+r'\s*(?:登记|表示|存储|记录|包含)([^。\n，,;；]{1,60})',document['text'])
        classification=re.search(r'(?:分类体系|实体类型|对象类型)(?:为|是|[:：])([^。\n]{1,60})',document['text'])
        local_text=declaration.group(1) if declaration else classification.group(1) if classification else document.get('heading','')
        for kind,pattern in checks:
            if kind in allowed and re.search(pattern,local_text,re.I):return kind
    # Automatic vocabulary requires a supported interpretation; an unknown
    # domain must not be forced into the analysis template by a generic fallback.
    return 'BusinessConcept' if 'BusinessConcept' in goal.entity_kinds else 'ReportItem' if 'ReportItem' in goal.entity_kinds else None

def _groups(table,goal):
    from .goal import column_is_excluded, column_is_evidence_only
    groups=defaultdict(lambda: {'code':None,'label':None,'aliases':[],'sort':[]})
    excluded=[]; scopes=[]; semantic={}
    for c in table.columns:
        name=c.name
        if column_is_excluded(goal,table,name) or SENSITIVE.search(name):
            excluded.append(ExcludedColumn(column=name,reason='sensitive_or_excluded'));continue
        if column_is_evidence_only(goal,table,name):
            excluded.append(ExcludedColumn(column=name,reason='evidence_only'));continue
        if SCOPE.fullmatch(name): scopes.append(name);continue
        if AUDIT.search(name): excluded.append(ExcludedColumn(column=name,reason='audit_metadata'));continue
        if SORT.search(name):
            excluded.append(ExcludedColumn(column=name,reason='display_order'));continue
        if name in SEMANTIC: semantic[SEMANTIC[name]]=name;continue
        alias=ALIAS.match(name); label=NAME.match(name); code=CODE.match(name)
        if alias: groups[alias[1].lower()]['aliases'].append(name)
        elif label: groups[label[1].lower()]['label']=name
        elif code: groups[code[1].lower()]['code']=name
    # Only matched code/label groups are structural entity candidates. A random ID
    # on an observation table does not turn every fact row into a semantic node.
    return groups,scopes,semantic,excluded


def _sample(adapter,table,columns,budget,limit=24):
    budget.admit(Cost(db_cu=1,memory_bytes=24576))
    rows=[]; size=0
    try:
        # Source-side LIMIT is only for exploration. Final RuleEngine reads the
        # complete joint DISTINCT under its own truncation/consistency controls.
        for batch in adapter.probe('sample_strata',table,{'columns':columns,'limit':limit}):
            try:
                for row in batch.rows():
                    record={c:v for c,v in zip(batch.columns,row)}
                    size+=len(canonical(record).encode())
                    if size>24576: raise LightGraphError('sample_memory_limit')
                    rows.append(record)
            finally: batch.release()
        return rows
    finally:
        budget.charge(Cost(db_cu=1,memory_bytes=min(size,24576)))


def _understanding(table,rows,roles,documents):
    columns=[]
    for c in table.columns:
        values=[r[c.name] for r in rows if c.name in r]
        counts={'sampled':len(values),'null':sum(v is None for v in values),'blank':sum(isinstance(v,str) and not v.strip() for v in values),
                'numeric':sum(isinstance(v,(int,float)) and not isinstance(v,bool) for v in values),
                'short_text':sum(isinstance(v,str) and 0<len(v)<=100 for v in values),
                'long_text':sum(isinstance(v,str) and len(v)>100 for v in values),
                'json_shape':sum(isinstance(v,str) and v.lstrip().startswith(('{','[')) for v in values)}
        conflicts=[]
        nonnull=counts['sampled']-counts['null']
        if nonnull and re.search(r'int|numeric|decimal|float',c.data_type,re.I) and counts['short_text']+counts['long_text']>nonnull/2:
            conflicts.append('declared_numeric_observed_text')
        if counts['json_shape'] and counts['json_shape']<nonnull: conflicts.append('mixed_json_shape')
        columns.append(ColumnUnderstanding(name=c.name,declared_type=c.data_type,comment_quality=_comment_quality(c.name,c.comment),
                       inferred_role=roles.get(c.name,'unknown'),observed_counts=counts,conflicts=conflicts))
    return DataUnderstanding(source=_source(table),dependency_hash=_table_hash(table),columns=columns,
        coverage='sampled' if rows else 'metadata_only',document_refs=sorted({d['doc_id']+'@'+d['revision'] for d in documents}),
        unknowns=['sample_does_not_establish_full_coverage'])


def _packet(table,goal,rows,documents,task,understanding):
    from .goal import column_is_excluded
    columns=[{'id':_column_id(table,c.name),'name':c.name,'type':c.data_type,'comment':c.comment,
              'observed':understanding.columns[i].observed_counts} for i,c in enumerate(table.columns)
              if not SENSITIVE.search(c.name) and not column_is_excluded(goal,table,c.name)]
    allowed_names={c['name'] for c in columns}
    safe_rows=[]
    for row in rows:
        record={}
        for name,value in row.items():
            if name not in allowed_names:continue
            # Do not silently truncate an identifier and then permit its use as evidence.
            if isinstance(value,str) and (len(value.encode())>200 or re.search(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|(?:^|\s)(?:\+?\d[ -]?){11,18}(?:$|\s)',value)): continue
            record[_column_id(table,name)]=value
        safe_rows.append(record)
    return {'packet_version':'1.0','task':task,'scope':_source(table).model_dump(by_alias=True),
            'semantic_schema':goal.semantic_schema.model_dump(mode='json'),
            'goal':goal.model_dump(),'metadata_digest':{'columns':columns},'probe_digest':[],
            'strata_samples':[{'stratum':'bounded_distinct_page','selection':'first_distinct_page','count':len(safe_rows),'rows':safe_rows}],
            'document_snippets':documents,'known_conflicts':[],
            'budget':{'max_hypotheses':6,'max_probe_requests':4,'max_rationale_chars':600}}


def _draft(table,group,scope,semantic,excluded,goal,documents):
    prefix,fields=group
    kind=_kind(prefix,table,goal,documents)
    if not kind:return None
    attrs=dict(semantic)
    if fields['label']:attrs['label']=fields['label']
    aliases={key:name for key,name in zip(('alias','english_name'),fields['aliases'])}
    # Semantic columns common to a multi-entity wide table cannot be assigned to
    # every entity without evidence; caller passes them only for one entity group.
    rule=RuleSpec(rule_id='rule_'+digest(table.source_id,table.qualified_name,'entity',prefix)[:24],
        kind='column_group_entity',goal_refs=[goal.goal_id],source=_source(table),
        scope=ScopeSpec(namespaces=scope),identity=IdentitySpec(key_columns=scope+[fields['code']],entity_kind=kind),
        attributes=AttributeSpec(single_valued=attrs,multi_valued=aliases,excluded=excluded),
        dependencies=Dependencies(schema=_table_hash(table),documents=sorted({d['doc_id']+'@'+d['revision'] for d in documents})),
        coverage=Coverage(scope='sampled',consistent=False),evidence=RuleEvidence(origin='discovery',semantic_basis=['declared_column_group']))
    sharing=declared_evidence(documents,table,'shared_definition',
        [attrs['definition']] if attrs.get('definition') else [],parameters={'namespace_columns':scope})
    if sharing and attrs.get('definition') and scope:
        rule.identity.semantic_resolution='verified_definition_registry'
        # A declaration document is a stable semantic namespace; labels and local
        # source codes remain properties/bindings and never define global identity.
        policy_document=sharing[0].split('@',1)[0]
        rule.identity.semantic_namespace='declared_definition:'+policy_document+':'+kind
        rule.evidence=rule_evidence(sharing)
    return rule


def _definition_document(document, table=None):
    applicable = (table is None or "sources" not in document
        or any(source.get("source_id") == table.source_id
               and source.get("schema", source.get("schema_name", "public")) == table.schema_name
               and source.get("table") == table.name for source in document["sources"]))
    return (applicable and document.get("evidence_role", "definition_evidence") == "definition_evidence"
        and document.get("read_state", "complete") == "complete"
        and not document.get("derived", False))


def _relative_hierarchy_contract(claim, documents, scopes, levels, table, reviewed_evidence=None):
    """Require an independently reviewed proposition or an exact declaration.

    No prescribed phrase, universal business code or document vocabulary is
    required from the Agent. Its proposed domain and position interpretation are
    checked together, before they may authorize shared identities.
    """
    parameters={name:getattr(claim,name) for name in ('identity_domain','identity_scope',
        'taxonomy','position_semantics','context_qualifiers','allow_dag')}
    if not reviewed_evidence:
        available={d['doc_id']+'@'+d['revision']+':'+d['section_id'] for d in documents if _definition_document(d,table)}
        if set(claim.identity_evidence+claim.attribute_evidence+claim.taxonomy_evidence
               +claim.position_evidence+claim.global_scope_evidence+claim.context_evidence+claim.dag_evidence)-available:
            raise LightGraphError('identity_domain_evidence_unresolved')
    evidence=reviewed_evidence or declared_evidence(documents,table,'relative_identity',
        [level.code for level in levels],parameters=parameters)
    if not evidence:raise LightGraphError('identity_domain_evidence_unresolved')
    if not claim.identity_domain or not claim.taxonomy or claim.position_semantics is None:
        raise LightGraphError('hierarchy_position_semantics_missing')
    identity = IdentitySpec(key_columns=scopes+[levels[0].code], entity_kind=claim.entity_kind_hint,
        semantic_resolution="verified_identity_domain", level_policy="relative_path",
        identity_domain=claim.identity_domain, identity_evidence=evidence,
        attribute_evidence=evidence, identity_scope=claim.identity_scope,
        global_scope_evidence=evidence if claim.identity_scope=='domain_global' else [])
    return identity, dict(taxonomy=claim.taxonomy, position_semantics=claim.position_semantics,
        position_evidence=evidence, context_qualifiers=claim.context_qualifiers,
        allow_dag=claim.allow_dag)


def _normalize_business_code_mapping(single, multi, key_columns):
    """The engine already emits the final identity component as business_code.

    Accept the model's redundant mapping only when it names that same column;
    otherwise the executor would silently overwrite a different source value.
    """
    for mapping in (single, multi):
        column = mapping.get('business_code')
        if column is None:
            continue
        if not key_columns or column != key_columns[-1]:
            raise LightGraphError('business_code_mapping_conflict')
        del mapping['business_code']


def _compile_model(batch,table,goal,documents,excluded, *, semantic_evidence=None):
    from lightgraph.store.policy import PersistencePolicy, SOURCE_PROPERTY_NAMES, KINDS
    lookup={_column_id(table,c.name):c.name for c in table.columns}
    semantic_schema=SemanticSchema.merged(goal.semantic_schema,batch.semantic_schema)
    declared_names={entry.name for entry in semantic_schema.entity_types}
    operator_types=SemanticSchema(entity_types=[EntityTypeDefinition(name=name,
        description=f'User-declared output type: {name}')
        for name in sorted(set(goal.entity_kinds)-KINDS['business']-declared_names)])
    semantic_schema=SemanticSchema.merged(semantic_schema,operator_types)
    properties=SOURCE_PROPERTY_NAMES | {
        definition.name for definition in semantic_schema.properties}
    from .goal import column_is_excluded, column_is_evidence_only
    # Name patterns are fallback hints only. Explicit model roles and operator
    # exclusions determine projection on the Agent path.
    excluded=list(excluded)
    for c in table.columns:
        if column_is_excluded(goal,table,c.name) or column_is_evidence_only(goal,table,c.name):
            excluded.append(ExcludedColumn(column=c.name,reason='outside_output_goal'))
    for p in batch.proposals:
        if p.kind=='exclusion':
            excluded.extend(ExcludedColumn(column=lookup[e.column_id],reason=e.reason)
                            for e in p.claim.exclusions if 'projection' in e.affects)
        elif p.kind=='column_group':
            excluded.extend(ExcludedColumn(column=lookup[m.column_id],reason=m.role)
                for m in p.claim.members if m.exclude_from_projection or m.role in ('display_order','audit')
                or (m.role=='fact_value' and not goal.observation_values))
    excluded=list({e.column:e for e in excluded}.values())
    excluded_names={e.column for e in excluded}
    identity_excluded={lookup[e.column_id] for p in batch.proposals if p.kind=='exclusion'
                       for e in p.claim.exclusions if 'identity' in e.affects}
    results=[]
    for proposal in batch.proposals:
        claim=proposal.claim
        if proposal.kind=='column_group':
            if goal.entity_kinds and claim.entity_kind_hint not in goal.entity_kinds:continue
            keys=list(dict.fromkeys(lookup[m.column_id] for m in claim.members if m.in_identity
                and (m.role=='identity_code' or (m.role=='semantic_attribute' and m.property_name=='business_code'))))
            namespaces=[lookup[m.column_id] for m in claim.members if m.role=='namespace']
            if not keys:continue
            if (excluded_names|identity_excluded) & set(keys+namespaces):raise LightGraphError('excluded_column_in_identity')
            labels=[lookup[m.column_id] for m in claim.members if m.role=='label' and lookup[m.column_id] not in excluded_names]
            aliases=[m for m in claim.members if m.role=='alias' and lookup[m.column_id] not in excluded_names]
            if len(labels)>1:raise LightGraphError('ambiguous_primary_label')
            single={'label':labels[0]} if labels else {}
            multi={}
            for index,member in enumerate(aliases):
                if index>=2:raise LightGraphError('too_many_alias_columns')
                name=member.property_name or ('alias','english_name')[index]
                if name not in ('alias','english_name') or name in multi:raise LightGraphError('ambiguous_alias_property')
                multi[name]=lookup[member.column_id]
            for member in claim.members:
                if member.role!='semantic_attribute' or lookup[member.column_id] in excluded_names:continue
                if member.property_name not in properties:
                    raise LightGraphError('unregistered_attribute_semantics')
                existing=single.get(member.property_name,multi.get(member.property_name))
                if existing is not None:
                    if existing==lookup[member.column_id]:continue
                    raise LightGraphError('ambiguous_primary_attribute')
                (multi if member.multi_value else single)[member.property_name]=lookup[member.column_id]
            _normalize_business_code_mapping(single,multi,list(dict.fromkeys(namespaces+keys)))
            results.append(RuleSpec(rule_id='rule_'+digest(table.source_id,table.qualified_name,'model_group',keys)[:24],kind='column_group_entity',
                goal_refs=[goal.goal_id],source=_source(table),scope=ScopeSpec(namespaces=namespaces),
                identity=IdentitySpec(key_columns=list(dict.fromkeys(namespaces+keys)),entity_kind=claim.entity_kind_hint),
                attributes=AttributeSpec(single_valued=single,
                    multi_valued=multi,excluded=excluded),
                dependencies=Dependencies(schema=_table_hash(table),documents=sorted({d['doc_id']+'@'+d['revision'] for d in documents})),
                coverage=Coverage(scope='sampled'),evidence=RuleEvidence(origin='discovery',semantic_basis=['model_column_group_candidate'])))
        elif proposal.kind=='entity_key':
            if goal.entity_kinds and claim.entity_kind_hint not in goal.entity_kinds:continue
            single=[lookup[c] for c in claim.attributes.single_valued]
            explicit={lookup[c]:name for c,name in claim.attributes.property_names.items()}
            if set(explicit.values())-properties:raise LightGraphError('unregistered_attribute_semantics')
            if len(single)>1 and any(c not in explicit and c not in SEMANTIC and not NAME.match(c) for c in single):
                raise LightGraphError('unregistered_attribute_semantics')
            single_map={}
            for column in single:
                property_name=explicit.get(column,SEMANTIC.get(column,'label'))
                if property_name in single_map:raise LightGraphError('ambiguous_primary_attribute')
                single_map[property_name]=column
            multi=[lookup[c] for c in claim.attributes.multi_valued]
            keys=[lookup[c] for c in claim.key_columns]
            if identity_excluded & set(keys):raise LightGraphError('excluded_column_in_identity')
            namespaces=[lookup[c] for c in claim.namespace_columns]
            if not set(namespaces)<=set(keys):raise LightGraphError('namespace_not_in_identity')
            if set(keys+single+multi)&excluded_names:raise LightGraphError('excluded_column_projected')
            if len(multi)>2:raise LightGraphError('too_many_alias_columns')
            multi_map={}
            for index,column in enumerate(multi):
                property_name=explicit.get(column,('alias','english_name')[index])
                if property_name in single_map or property_name in multi_map:raise LightGraphError('ambiguous_primary_attribute')
                multi_map[property_name]=column
            _normalize_business_code_mapping(single_map,multi_map,keys)
            results.append(RuleSpec(rule_id='rule_'+digest(table.source_id,table.qualified_name,'model_key',keys)[:24],kind='column_group_entity',
                goal_refs=[goal.goal_id],source=_source(table),scope=ScopeSpec(namespaces=namespaces),
                identity=IdentitySpec(key_columns=keys,entity_kind=claim.entity_kind_hint),
                attributes=AttributeSpec(single_valued=single_map,
                    multi_valued=multi_map,excluded=excluded),
                dependencies=Dependencies(schema=_table_hash(table),documents=sorted({d['doc_id']+'@'+d['revision'] for d in documents})),coverage=Coverage(scope='sampled'),
                evidence=RuleEvidence(origin='discovery',semantic_basis=['model_entity_key_candidate'])))
        elif proposal.kind=='long_hierarchy':
            if goal.entity_kinds and claim.entity_kind_hint not in goal.entity_kinds:continue
            scopes=[lookup[c] for c in claim.namespace_columns]
            key,parent=lookup[claim.key_column],lookup[claim.parent_column]
            label=lookup.get(claim.label_column)
            aliases=[lookup[c] for c in claim.alias_columns]
            if set(scopes+[key])&identity_excluded:raise LightGraphError('excluded_column_in_identity')
            if set(scopes+[key,parent]+([label] if label else [])+aliases)&excluded_names:
                raise LightGraphError('excluded_column_projected')
            order=semantic_evidence or declared_evidence(documents,table,'parent_column',[key],[parent])
            results.append(RuleSpec(rule_id='rule_'+digest(table.source_id,table.qualified_name,'model_parent',scopes,key,parent)[:24],
                kind='long_hierarchy',goal_refs=[goal.goal_id],source=_source(table),scope=ScopeSpec(namespaces=scopes),
                identity=IdentitySpec(key_columns=scopes+[key],entity_kind=claim.entity_kind_hint),
                attributes=AttributeSpec(single_valued={'label':label} if label else {},
                    multi_valued=dict(zip(('alias','english_name'),aliases)),excluded=excluded),
                hierarchy=HierarchySpec(parent_column=parent,taxonomy=table.qualified_name,order_evidence=order),
                dependencies=Dependencies(schema=_table_hash(table),documents=sorted({d['doc_id']+'@'+d['revision'] for d in documents})),
                coverage=Coverage(scope='sampled'),evidence=rule_evidence(order,reviewed=bool(semantic_evidence),documents=documents)))
        elif proposal.kind=='wide_hierarchy':
            if goal.entity_kinds and claim.entity_kind_hint not in goal.entity_kinds:continue
            levels=[LevelSpec(level=l.position,code=lookup[l.key],label=lookup.get(l.label),aliases=[lookup[l.alias]] if l.alias else []) for l in claim.levels]
            scopes=[lookup[c] for c in claim.namespace_columns]
            if identity_excluded & set(scopes+[l.code for l in levels]):raise LightGraphError('excluded_column_in_identity')
            if set(scopes+[c for l in levels for c in [l.code,l.label,*l.aliases] if c])&excluded_names:
                raise LightGraphError('excluded_column_projected')
            level_columns=[level.code for level in levels]
            order=semantic_evidence or declared_evidence(documents,table,'ordered_hierarchy',level_columns)
            fill=(semantic_evidence if claim.repeat_policy=='collapse_consecutive_equal_identity' else None
                ) or declared_evidence(documents,table,'collapse_consecutive_equal_identity',level_columns)
            identity=IdentitySpec(key_columns=scopes+[levels[0].code],entity_kind=claim.entity_kind_hint,
                level_policy='code_with_fill_merge' if fill else 'level_qualified')
            hierarchy_fields={"taxonomy":table.qualified_name}
            if claim.path_semantics == "relative_path":
                identity,hierarchy_fields=_relative_hierarchy_contract(claim,documents,scopes,levels,table,semantic_evidence)
                if not order:raise LightGraphError("hierarchy_order_evidence_unresolved")
            results.append(RuleSpec(rule_id='rule_'+digest(table.source_id,table.qualified_name,'model_hierarchy',[l.code for l in levels])[:24],
                kind='wide_hierarchy',goal_refs=[goal.goal_id],source=_source(table),scope=ScopeSpec(namespaces=scopes),
                identity=identity,
                hierarchy=HierarchySpec(levels=levels,fill_semantics_confirmed=bool(fill),order_evidence=order,**hierarchy_fields),
                dependencies=Dependencies(schema=_table_hash(table),documents=sorted({d['doc_id']+'@'+d['revision'] for d in documents})),
                coverage=Coverage(scope='sampled'),evidence=rule_evidence(order,reviewed=bool(semantic_evidence),documents=documents)))
    for rule in results:
        used_properties=set(rule.attributes.single_valued)|set(rule.attributes.multi_valued)|{'business_code'}
        if rule.hierarchy:
            used_properties.update({'label','alias'})
        relation=rule.relation.relation if rule.relation else 'parent_of' if rule.hierarchy else None
        local_schema=semantic_schema.for_usage(entity_kinds={rule.identity.entity_kind},
            properties=used_properties,relations={relation} if relation else set())
        # Store only new contracts. Compatible built-in declarations are
        # references to existing contracts, not schema redefinitions.
        rule.semantic_schema=PersistencePolicy().with_schema(local_schema).semantic_schema
    return results


def discover(adapter,goal: ExtractionGoal | None=None,documents=None,model=None,budget=None, *,
             table_refs=None,iteration_context=None,revision_rules=None) -> DiscoveryResult:
    """Discover bounded candidates, preserving the source/semantic validation boundary.

    No dataset mappings are imported. Generic column families and sample shapes
    seed deterministic proposals; optional T1/T3 model tasks interpret ambiguities.
    Every RuleSpec remains draft. Only a complete execution validator can publish.
    """
    if model is not None and callable(getattr(model,'explore',None)):
        from .workflow import discover_with_agent
        return discover_with_agent(adapter,goal,documents,model,budget,table_refs=table_refs,
            iteration_context=iteration_context,revision_rules=revision_rules)
    goal=goal or ExtractionGoal();budget=budget or (model.budget if model else BudgetAllocator())
    corpus=documents if isinstance(documents,DocumentCorpus) else DocumentCorpus(documents or [])
    result=DiscoveryResult();tables=adapter.describe();used_tables=[]
    from .goal import source_in_goal
    for table in tables:
        if not source_in_goal(goal,table):continue
        if table_refs is not None and not (
                (table.source_id,table.schema_name,table.name) in table_refs
                or f"{table.source_id}.{table.schema_name}.{table.name}" in table_refs
                or table.qualified_name in table_refs):
            continue
        used_tables.append(table)
        reservation='validate_'+digest(table.source_id,table.qualified_name)[:16]
        try:
            budget.reserve(reservation,Cost(db_cu=2))
        except BudgetExceeded as exc:
            result.issues.append({'table':table.qualified_name,'code':exc.code,'status':'deferred'});continue
        rows=[]
        try:
            groups,scopes,semantic,excluded=_groups(table,goal)
            excluded_names={c.column for c in excluded}
            sample_columns=[c.name for c in table.columns if c.name not in excluded_names][:64]
            docs=corpus.context_for(_source(table),sample_columns)
            if len(table.columns)>64:result.issues.append({'table':table.qualified_name,'code':'sample_column_limit','status':'partial'})
            if sample_columns: rows=_sample(adapter,table,sample_columns,budget)
            roles={c:'namespace' for c in scopes}
            for entry in groups.values():
                if entry['code']:roles[entry['code']]='identity_code'
                if entry['label']:roles[entry['label']]='label'
                roles.update({c:'alias' for c in entry['aliases']})
            roles.update({c.column:'display_order' if c.reason=='display_order' else 'audit' if c.reason=='audit_metadata' else 'unknown' for c in excluded})
            understanding=_understanding(table,rows,roles,docs);result.understandings.append(understanding)
            candidates=[(p,g) for p,g in groups.items() if g['code'] and g['label']]
            table_rules=[];hierarchy_families=defaultdict(list)
            for p,g in candidates:
                match=LEVEL.search(p)
                if match:
                    family=LEVEL.sub(r'\1level',p)
                    hierarchy_families[family].append((int(match[2]),p,g))
            hierarchical_prefixes=set()
            for family,entries in hierarchy_families.items():
                if len(entries)<2:continue
                entries.sort();hierarchical_prefixes.update(p for _,p,_ in entries)
                levels=[LevelSpec(level=n,code=g['code'],label=g['label'],aliases=g['aliases']) for n,_,g in entries]
                kind=_kind(family,table,goal,docs)
                if kind is None:continue
                level_columns=[level.code for level in levels]
                order=declared_evidence(docs,table,'ordered_hierarchy',level_columns)
                fill=declared_evidence(docs,table,'collapse_consecutive_equal_identity',level_columns)
                table_rules.append(RuleSpec(rule_id='rule_'+digest(table.source_id,table.qualified_name,'hierarchy',family)[:24],
                    kind='wide_hierarchy',goal_refs=[goal.goal_id],source=_source(table),scope=ScopeSpec(namespaces=scopes),
                    identity=IdentitySpec(key_columns=scopes+[levels[0].code],entity_kind=kind,
                        level_policy='code_with_fill_merge' if fill else 'level_qualified'),
                    attributes=AttributeSpec(excluded=excluded),
                    hierarchy=HierarchySpec(levels=levels,taxonomy=table.qualified_name,order_evidence=order,fill_semantics_confirmed=bool(fill)),
                    dependencies=Dependencies(schema=_table_hash(table),documents=sorted({d['doc_id']+'@'+d['revision'] for d in docs})),
                    coverage=Coverage(scope='sampled'),evidence=rule_evidence(order)))
                if not order: result.issues.append({'table':table.qualified_name,'code':'hierarchy_order_semantics_unresolved','status':'unresolved'})
            for p,g in candidates:
                if p in hierarchical_prefixes:continue
                rule=_draft(table,(p,g),scopes,semantic if len(candidates)==1 else {},excluded,goal,docs)
                if rule:
                    # Parent-column declarations need document support; the name is
                    # a proposal and full cycle/parent checks remain mandatory.
                    parent_candidates=[c.name for c in table.columns if c.name not in excluded_names and re.fullmatch(r'parent_(code|id)|父(?:编码|编号)',c.name,re.I)]
                    order=(declared_evidence(docs,table,'parent_column',[g['code']],parent_candidates)
                        if len(parent_candidates)==1 else [])
                    if len(parent_candidates)==1 and order:
                        rule.hierarchy=HierarchySpec(parent_column=parent_candidates[0],taxonomy=table.qualified_name,order_evidence=order);rule.kind='long_hierarchy'
                        rule.evidence=rule_evidence(order)
                    table_rules.append(rule)
            if model:
                tasks=['T1_table_discovery']
                if any(len(x)>=2 for x in hierarchy_families.values()):tasks.append('T3_hierarchy')
                for task in tasks:
                    try:
                        packet=_packet(table,goal,rows if getattr(model.config,'allow_source_samples',False) else [],docs,task,understanding)
                        batch=model.propose(packet)
                        proposed=_compile_model(batch,table,goal,docs,excluded)
                        # A model proposal replaces only the matching key family,
                        # preserving unrelated deterministic candidates.
                        for rule in proposed:
                            existing=[r for r in table_rules if r.kind==rule.kind and r.identity.key_columns==rule.identity.key_columns]
                            if existing:table_rules=[r for r in table_rules if r not in existing]
                            table_rules.append(rule)
                        if batch.abstain:result.issues.append({'table':table.qualified_name,'code':'model_abstained','status':'unresolved'})
                    except LightGraphError as exc:
                        result.issues.append({'table':table.qualified_name,'code':exc.code,'status':'deferred' if isinstance(exc,BudgetExceeded) else 'unresolved'})
            if not table_rules:result.issues.append({'table':table.qualified_name,'code':'no_confirmable_entity_group','status':'unresolved'})
            checked_rules=[]
            conflicting_columns={c.name for c in understanding.columns if c.conflicts}
            for rule in table_rules:
                identity_and_label=set(rule.identity.key_columns) | {rule.attributes.single_valued.get('label')}
                if identity_and_label & conflicting_columns:
                    result.issues.append({'table':table.qualified_name,'code':'schema_value_conflict','status':'unresolved'})
                    continue
                label=rule.attributes.single_valued.get('label')
                if rule.kind=='column_group_entity' and label:
                    try:
                        attribute_columns=list(dict.fromkeys(rule.attributes.single_valued.values()))
                        check_cost=Cost(db_cu=len(attribute_columns)+1)
                        budget.admit(check_cost,reservation)
                        try:check=adapter.check_fd(table,rule.identity.key_columns,attribute_columns)
                        finally:budget.charge(check_cost,reservation)
                        conflicts=int(check.get('violating_groups',0))
                        if conflicts:
                            if rule.identity.semantic_resolution=='verified_definition_registry':
                                rule.identity.semantic_resolution='source_scoped';rule.identity.semantic_namespace=''
                            result.issues.append({'table':table.qualified_name,'code':'fd_violation','count':conflicts,'status':'unresolved'})
                            if model:
                                h=Hypothesis(hypothesis_id=Hypothesis.identity(rule.source,rule.kind,rule.identity.key_columns),
                                    kind='column_group',source=rule.source,columns=rule.identity.key_columns,status='counterexample',
                                    codes=['fd_violation'])
                                packet=_packet(table,goal,rows if getattr(model.config,'allow_source_samples',False) else [],docs,'T5_revision',understanding)
                                packet['probe_digest']=[{'probe_id':'fd_check','operator':'check_fd','aggregates':{'violating_groups':conflicts}}]
                                revised,batch=revise_after_counterexample(model,packet,h,[{'code':'fd_violation','count':conflicts,'probe_id':'fd_check'}])
                                result.hypotheses.append(revised)
                                if batch:
                                    replacements=_compile_model(batch,table,goal,docs,excluded)
                                    safe_replacements=[]
                                    for replacement in replacements:
                                        original_keys=set(rule.identity.key_columns)
                                        replacement_keys=set(replacement.identity.key_columns)
                                        # A counterexample cannot be hidden by dropping
                                        # the disputed label or putting its value into
                                        # the identity. Namespace/version expansion is
                                        # a genuine candidate and is revalidated later.
                                        preserves_attributes=all(replacement.attributes.single_valued.get(k)==v for k,v in rule.attributes.single_valued.items())
                                        added=replacement_keys-original_keys
                                        valid_added=all(not NAME.match(c) and not SORT.search(c) for c in added)
                                        if not original_keys<=replacement_keys or not preserves_attributes or not valid_added:
                                            result.issues.append({'table':table.qualified_name,'code':'revision_masks_counterexample','status':'unresolved'});continue
                                        if not added:
                                            result.issues.append({'table':table.qualified_name,'code':'revision_no_structural_change','status':'unresolved'});continue
                                        safe_replacements.append(replacement)
                                    if safe_replacements:
                                        checked_rules.extend(safe_replacements);continue
                    except LightGraphError as exc:
                        if rule.identity.semantic_resolution=='verified_definition_registry':
                            rule.identity.semantic_resolution='source_scoped';rule.identity.semantic_namespace=''
                        result.issues.append({'table':table.qualified_name,'code':exc.code,'status':'deferred' if isinstance(exc,BudgetExceeded) else 'unresolved'})
                checked_rules.append(rule)
            for rule in checked_rules:
                hypothesis=Hypothesis(hypothesis_id=Hypothesis.identity(rule.source,rule.kind,rule.identity.key_columns),
                    kind='wide_hierarchy' if rule.kind=='wide_hierarchy' else 'column_group',source=rule.source,
                    columns=rule.identity.key_columns,status='proposed',evidence_level='L2_sampled' if rows else 'L0_declared',
                    created_by='llm' if 'model_' in ' '.join(rule.evidence.semantic_basis) else 'rule',rule_id=rule.rule_id,
                    dependencies={'schema':_table_hash(table)})
                rule.evidence.hypothesis=hypothesis.hypothesis_id
                understanding.rule_refs.append(rule.rule_id)
                result.hypotheses.append(hypothesis);result.rules.append(rule)
        except LightGraphError as exc:
            result.issues.append({'table':table.qualified_name,'code':exc.code,'status':'deferred' if isinstance(exc,BudgetExceeded) else 'unresolved'})
        finally:
            rows.clear();budget.release(reservation)
    result.stats={'tables_considered':len(used_tables),'rules_proposed':len(result.rules),
                  'hypotheses':len(result.hypotheses),'model_calls':model.usage['calls'] if model else 0,
                  'semantic_validation':'requires_rule_engine','coverage':'sampled'}
    result.budget=budget.snapshot()
    return result

class DiscoveryEngine:
    def __init__(self,adapter,goal=None,model=None,budget=None,documents=None):
        self.adapter,self.goal,self.model,self.budget,self.documents=adapter,goal,model,budget,documents
    def discover(self):return discover(self.adapter,self.goal,self.documents,self.model,self.budget)


def invalidate_dependencies(hypotheses: Iterable[Hypothesis], current: dict[str,str]) -> list[Hypothesis]:
    updated=[]
    for h in hypotheses:
        copy=h.model_copy(deep=True)
        if any(current.get(k)!=v for k,v in h.dependencies.items()):
            copy.status='stale';copy.codes=list(dict.fromkeys(copy.codes+['dependency_changed']))
        updated.append(copy)
    return updated


def revise_after_counterexample(model,packet:dict,hypothesis:Hypothesis,counterexamples:list[dict],*,max_revisions=1):
    """T5 is entered only after a verifier reports a counterexample, never routinely."""
    if hypothesis.status!='counterexample' or not counterexamples:
        raise LightGraphError('revision_requires_counterexample')
    if hypothesis.revision>max_revisions:
        revised=hypothesis.model_copy(deep=True);revised.status='unresolved';revised.codes.append('revision_limit')
        return revised,None
    codes=[]
    for counter in counterexamples:
        if set(counter)-{'code','count','probe_id'} or not isinstance(counter.get('count'),int):
            raise LightGraphError('unsafe_counterexample_summary')
        codes.append(counter['code'])
    revised_packet={**packet,'task':'T5_revision','known_conflicts':counterexamples,
                    'revision_of':hypothesis.hypothesis_id,'allowed_revision_codes':codes}
    proposals=model.propose(revised_packet)
    revised=hypothesis.model_copy(deep=True);revised.revision+=1;revised.revision_of=hypothesis.hypothesis_id
    if proposals.abstain or not proposals.proposals:
        revised.status='unresolved';revised.codes.append('revision_abstained');return revised,None
    revised.hypothesis_id='h_'+digest(hypothesis.hypothesis_id,revised.revision,[p.claim.model_dump() for p in proposals.proposals])[:24]
    revised.status='revised'
    return revised,proposals


def discover_references(adapter,validated_rules,documents=None,model=None,budget=None,*,max_candidates=32,goal=None,
                        source_adapters=None,table_refs=None,iteration_context=None,revision_rules=None) -> DiscoveryResult:
    """Reference discovery joins already verified entity bindings, never all columns.

    Structural coverage and document semantics are separate. Exact column-family
    matches only nominate candidates; SELECT aggregates verify composite keys.
    Tables with two recognized endpoints may emit documented mapping relations.
    Undocumented overlap remains an unresolved candidate, never business truth.
    """
    if model is not None and callable(getattr(model,'explore',None)):
        from .workflow import discover_references_with_agent
        result=discover_references_with_agent(adapter,validated_rules,documents,model,budget,goal,max_candidates,
            source_adapters=source_adapters,table_refs=table_refs,
            iteration_context=iteration_context,revision_rules=revision_rules)
        # Preserve the existing independently documented JSON/selector rules.
        # Agent proposals cannot invent selector constants; this deterministic
        # verifier still requires a scoped declaration and complete key checks.
        if not any(i.get('status')=='deferred' for i in result.issues):
            from .references import discover_complex_references
            from lightgraph.sources.scoped import GoalScopedSource
            active_budget=budget or model.budget
            corpus=documents if isinstance(documents,DocumentCorpus) else DocumentCorpus(documents or [])
            remaining=max_candidates-int(result.stats.get('reference_candidates',0))
            if remaining>0:
                scoped=GoalScopedSource(adapter,goal or ExtractionGoal())
                try:extra=discover_complex_references(scoped,validated_rules,corpus,active_budget,max_candidates=remaining)
                finally:scoped.close()
                result.rules.extend(extra.rules);result.issues.extend(extra.issues);result.stats.update(extra.stats)
                result.stats['rules_proposed']=len(result.rules);result.budget=active_budget.snapshot()
        return result
    budget=budget or (model.budget if model else BudgetAllocator())
    corpus=documents if isinstance(documents,DocumentCorpus) else DocumentCorpus(documents or [])
    result=DiscoveryResult();tables=adapter.describe()
    catalog={(t.source_id,t.schema_name,t.name):t for t in tables}
    approved=[r for r in validated_rules if r.status in ('validated','canary_checked','published') and r.validation_ref and r.kind=='column_group_entity']
    seen=set();count=0
    for table in tables:
        if count>=max_candidates:break
        refs=[]
        for rule in approved:
            if rule.source.source_id != table.source_id:continue
            keys=rule.identity.key_columns
            if set(keys)<=set(table.column_names):refs.append(rule)
        docs=corpus.context_for(_source(table),table.column_names)
        # Skip unbounded pair generation. A source table must expose at least two
        # distinct registered endpoint key families for relation-table inference.
        if len(refs)<2:continue
        if len(refs)>8:
            result.issues.append({'table':table.qualified_name,'code':'reference_family_ambiguous','status':'unresolved'});continue
        for i,source_rule in enumerate(refs):
            for target_rule in refs[i+1:]:
                if source_rule.identity.key_columns==target_rule.identity.key_columns:continue
                if count>=max_candidates:break
                source_endpoint,target_endpoint=source_rule,target_rule
                pair=(table.qualified_name,source_endpoint.rule_id,target_endpoint.rule_id)
                if pair in seen:continue
                seen.add(pair);count+=1
                reservation='reference_'+digest(pair)[:16]
                try:
                    budget.reserve(reservation,Cost(db_cu=2))
                    checks=[]
                    for binding in (source_endpoint,target_endpoint):
                        target=catalog.get((binding.source.source_id,binding.source.schema_name,binding.source.table))
                        if target is None:raise LightGraphError('unavailable_reference_target')
                        budget.admit(Cost(db_cu=1),reservation)
                        try:
                            checks.append(adapter.check_reference(table,binding.identity.key_columns,target,binding.identity.key_columns))
                        finally:budget.charge(Cost(db_cu=1),reservation)
                    stats=[{k:v for k,v in check.items() if isinstance(v,(int,float,bool))} for check in checks]
                    if any(check.get('unmatched',0) or check.get('null_refs',0) or check.get('ambiguous',0) for check in checks):
                        result.issues.append({'table':table.qualified_name,'code':'reference_missing','status':'unresolved'});continue
                    relation='verified_reference'
                    evidence=[]
                    # No NLP is attempted by the no-model execution path.
                    # A declaration binds exact ordered columns and direction.
                    for left,right in ((source_endpoint,target_endpoint),(target_endpoint,source_endpoint)):
                        for predicate in ((goal or ExtractionGoal()).relation_kinds or
                                          ['parent_of','member_of','uses_measure','verified_reference']):
                            matches=declared_evidence(docs,table,predicate,
                                left.identity.key_columns,right.identity.key_columns)
                            if matches:
                                source_endpoint,target_endpoint=left,right
                                relation,evidence=predicate,matches
                                break
                        if evidence:break
                    if not evidence:
                        result.issues.append({'table':table.qualified_name,'code':'technical_overlap_semantics_unresolved','status':'unresolved'})
                    source_columns=list(source_endpoint.identity.key_columns);target_columns=list(target_endpoint.identity.key_columns)
                    rule=RuleSpec(rule_id='rule_'+digest(table.source_id,pair,'reference')[:24],kind='reference',source=_source(table),
                        scope=ScopeSpec(namespaces=source_endpoint.scope.namespaces),
                        identity=IdentitySpec(key_columns=source_columns,entity_kind=source_endpoint.identity.entity_kind),
                        relation=RelationSpec(source_rule_id=source_endpoint.rule_id,target_rule_id=target_endpoint.rule_id,
                            source_columns=source_columns,target_columns=target_columns,relation=relation),
                        dependencies=Dependencies(schema=_table_hash(table),documents=sorted({d['doc_id']+'@'+d['revision'] for d in docs})),
                        evidence=rule_evidence(evidence,probe_refs=['reference_'+digest(pair)[:16]]),
                        coverage=Coverage(scope='full',consistent=False))
                    result.rules.append(rule)
                    if model:
                        # T2 may interpret semantics, but cannot bypass preverified
                        # endpoint registration or make its own selector values.
                        packet={'packet_version':'1.0','task':'T2_cross_table',
                            'scope':_source(table).model_dump(by_alias=True),
                            'metadata_digest':{'columns':[{'id':_column_id(table,c.name),'name':c.name,'type':c.data_type} for c in table.columns if not SENSITIVE.search(c.name)]},
                            'probe_digest':[{'probe_id':'reference_check','operator':'check_reference','aggregates':{'endpoint_count':2}}],
                            'strata_samples':[],'document_snippets':docs,'authorized_entities':[r.rule_id for r in refs],
                            'authorized_selectors':[],'known_conflicts':[]}
                        proposal=model.propose(packet)
                        if any(p.kind=='discriminator' for p in proposal.proposals):
                            result.issues.append({'table':table.qualified_name,'code':'discriminator_requires_registered_selectors','status':'unresolved'})
                except LightGraphError as exc:
                    result.issues.append({'table':table.qualified_name,'code':exc.code,'status':'deferred' if isinstance(exc,BudgetExceeded) else 'unresolved'})
                finally:budget.release(reservation)
    if count>=max_candidates:
        result.issues.append({'code':'reference_candidate_limit','status':'partial'})
    from .references import discover_complex_references
    complex_result=discover_complex_references(adapter,approved,corpus,budget,max_candidates=max(1,max_candidates-count))
    result.rules.extend(complex_result.rules);result.issues.extend(complex_result.issues)
    result.stats={'reference_candidates':count,'rules_proposed':len(result.rules),'endpoint_rules':len(approved),**complex_result.stats}
    result.budget=budget.snapshot();return result
