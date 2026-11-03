"""Versioned field/object mappings retained across bounded discovery sessions.

Only authorized metadata, fixed roles, counts and issued document references are
stored. A remembered hypothesis never becomes evidence for its own meaning.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from itertools import combinations, product
import json
import re
from typing import Literal

from pydantic import Field, model_validator

from lightgraph.contracts import StrictModel, SourceRef, ExtractionGoal, canonical, digest
from lightgraph.context import context_dependency_fingerprint
from lightgraph.errors import LightGraphError


class ObjectPosition(StrictModel):
    position: int = Field(ge=1, le=64)
    code: str
    label: str | None = None
    aliases: list[str] = Field(default_factory=list, max_length=32)


class ObjectModelDraft(StrictModel):
    table_id: str = Field(default='', max_length=256, description=
        'Current task table only: source_id:schema.table, schema.table, or the table name from this packet. '
        'Column IDs must come from this table. Do not copy another table\'s columns.')
    object_id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_]{0,63}$', description=
        'Local hypothesis identifier, not a source value or business identity. Reuse an agent candidate '
        'ID to revise it; use a new ID when explicitly superseding metadata candidates.')
    key_columns: list[str] = Field(default_factory=list, max_length=16, description=
        'Issued code/key column IDs. For relative hierarchy positions, list their code columns; '
        'this records their roles and does not make a composite entity key.')
    label_columns: list[str] = Field(default_factory=list, max_length=16)
    english_name_columns: list[str] = Field(default_factory=list, max_length=32)
    alias_columns: list[str] = Field(default_factory=list, max_length=32)
    namespace_columns: list[str] = Field(default_factory=list, max_length=16, description=
        'Issued columns proposed to limit object identity. Candidate mappings may add scope columns, '
        'but supersession must retain every previously mapped namespace column. '
        'Sharing a namespace column across objects is allowed. Validated scope changes require rule revision.')
    positions: list[ObjectPosition] = Field(default_factory=list, max_length=32)
    evidence_refs: list[str] = Field(default_factory=list, max_length=32, description=
        'Issued definition sections for this table only.')
    position_semantics: Literal['ordinal_slots', 'ordered_slots'] | None = Field(default=None, description=
        'Set only when this table\'s definition says how positions work. ordinal_slots keeps numbered positions '
        'and treats repeated codes as fill. ordered_slots means a dense order. Omit it when the definition does not say.')
    supersedes_object_ids: list[str] = Field(default_factory=list, max_length=32, description=
        'Explicit issued IDs of metadata_name_family candidates replaced by this object model. '
        'Preserve each candidate\'s complete code/name/English/alias/namespace mapping and every '
        'code/name/alias position tuple. Shared columns alone never authorize replacement. '
        'Previously validated or published objects require rule revision, not supersession.')

    @model_validator(mode='after')
    def field_roles(self):
        if not self.key_columns and not self.positions:
            raise ValueError('object_model_identity_fields_required')
        keys=set(self.key_columns)|{p.code for p in self.positions}
        labels=set(self.label_columns)|set(self.english_name_columns)|set(self.alias_columns)
        labels|={p.label for p in self.positions if p.label}
        labels|={c for p in self.positions for c in p.aliases}
        if keys & labels:
            raise ValueError('object_model_code_label_overlap')
        if len({p.position for p in self.positions})!=len(self.positions):
            raise ValueError('object_model_duplicate_position')
        return self


def _field_ids(model):
    result=set(model.key_columns+model.label_columns+model.english_name_columns+
               model.alias_columns+model.namespace_columns)
    for position in model.positions:
        result.add(position.code)
        if position.label:result.add(position.label)
        result.update(position.aliases)
    return result


def _mapped_columns(objects):
    refs=set()
    for obj in objects:
        if obj['status'] in {'conflict','superseded'}:continue
        for field in ('key_columns','label_columns','english_name_columns','alias_columns','namespace_columns'):
            refs.update(obj.get(field,[]))
        for position in obj.get('positions',[]):
            refs.add(position['code'])
            if position.get('label'):refs.add(position['label'])
            refs.update(position.get('aliases',[]))
    return refs


def current_object_knowledge(knowledge):
    """Return a prompt view without retired hypotheses; keep the ledger intact."""
    value=deepcopy(knowledge)
    retired=sum(o.get('status')=='superseded' for o in value.get('objects',[]))
    value['retired_object_ids']=sorted(set(value.get('retired_object_ids',[]))|
        {o['object_id'] for o in value.get('objects',[]) if o.get('status')=='superseded'})
    value['objects']=[o for o in value.get('objects',[]) if o.get('status')!='superseded']
    value['objects_total']=len(value['objects'])+value.get('objects_omitted',0)
    value['objects_superseded']=value.get('objects_superseded',0)+retired
    return value


def _mapping_loss_diagnostics(previous,incoming):
    """Explain lost associations using issued field references, never row values."""
    diagnostics=[]
    def missing(path,required,*,constraint='mapping_preservation',position=None):
        diagnostic={'path':path,'constraint':constraint,'required_references':sorted(required)}
        if position is not None:diagnostic['position']=position
        diagnostics.append(diagnostic)
    def role_refs(value,field):
        refs=set(value.get(field,[]))
        if field in {'key_columns','label_columns'}:
            refs.update(p.get('code' if field=='key_columns' else 'label') for p in value.get('positions',[]))
            refs.discard(None)
        return refs
    fields=('key_columns','label_columns','english_name_columns','alias_columns')
    for field in fields:
        absent=role_refs(previous,field)-role_refs(incoming,field)
        if absent:missing([field],absent)
    # These are candidate field roles, not verified identity scopes. A naming
    # heuristic may omit a scope column; adding it loses no remembered mapping.
    # Removal/replacement still loses scope, and verified objects are rejected
    # by apply_object_model before this candidate-preservation check is reached.
    absent=set(previous.get('namespace_columns',[]))-set(incoming.get('namespace_columns',[]))
    if absent:
        missing(['namespace_columns'],absent,constraint='preserve_identity_scope')
    positions={p['position']:(i,p) for i,p in enumerate(incoming.get('positions',[]))}
    for old in previous.get('positions',[]):
        index,new=positions.get(old['position'],(None,None))
        if new is None:
            missing(['positions'],[old['code']]+([old['label']] if old.get('label') else [])+
                old.get('aliases',[]),constraint='missing_position',position=old['position'])
            continue
        for field in ('code','label'):
            if new.get(field)!=old.get(field):
                missing(['positions',index,field],[old[field]] if old.get(field) else [],
                    constraint='same_position_mapping',position=old['position'])
        absent=set(old.get('aliases',[]))-set(new.get('aliases',[]))
        if absent:missing(['positions',index,'aliases'],absent,position=old['position'])
    if not previous.get('positions'):
        # An unpositioned code/name pair cannot be swallowed by a wider flat
        # list: that list no longer tells which name belongs to which code.
        same_keys=role_refs(previous,'key_columns')==role_refs(incoming,'key_columns')
        if not same_keys:
            missing(['key_columns'],role_refs(previous,'key_columns'),constraint='same_object_key')
    return diagnostics


def _retains_mapping(previous,incoming):
    """Preserve role associations, not merely the union of referenced columns."""
    return not _mapping_loss_diagnostics(previous,incoming)


def object_model_mapping_diagnostics(knowledge,model):
    """Bounded correction hints for an already rejected supersession attempt."""
    incoming=model.model_dump(mode='json')
    indexed={obj['object_id']:obj for obj in knowledge.get('objects',[])}
    previous=indexed.get(model.object_id)
    targets=list(dict.fromkeys(model.supersedes_object_ids))
    if previous and previous.get('supersedes_object_ids'):targets.append(model.object_id)
    diagnostics=[]
    for identifier in targets:
        if identifier not in indexed:continue
        for diagnostic in _mapping_loss_diagnostics(indexed[identifier],incoming):
            diagnostics.append({**diagnostic,'object_id':identifier})
            if len(diagnostics)==8:return diagnostics
    return diagnostics


def apply_object_model(knowledge,model,*,basis='agent_field_mapping'):
    """Atomically revise a candidate and explicitly retire compatible seeds.

    The owning knowledge snapshot supplies source and dependency version. Only
    IDs present in that snapshot are eligible; callers cannot grant ownership
    through model arguments. The unchanged input also makes rejection atomic.
    """
    model=ObjectModelDraft.model_validate(model)
    if model.object_id in knowledge.get('retired_object_ids',[]):
        raise LightGraphError('object_model_already_superseded')
    incoming=model.model_dump(mode='json')
    objects=knowledge.get('objects',[])
    indexed={obj['object_id']:obj for obj in objects}
    prior=indexed.get(model.object_id)
    if prior:
        if prior.get('basis')=='executed_rule' or prior.get('status') in {'local_validated','published'}:
            raise LightGraphError('object_model_verified_revision_required')
        if prior.get('status')=='superseded':
            raise LightGraphError('object_model_already_superseded')
        if prior.get('basis')=='metadata_name_family':
            raise LightGraphError('object_model_supersession_requires_new_id')
    inherited=list(prior.get('supersedes_object_ids',[])) if prior else []
    requested=[identifier for identifier in dict.fromkeys(model.supersedes_object_ids)
               if identifier not in inherited]
    for identifier in requested:
        if identifier==model.object_id:raise LightGraphError('object_model_supersession_requires_new_id')
        previous=indexed.get(identifier)
        if previous is None:raise LightGraphError('object_model_supersession_target_invalid')
        if previous.get('source',knowledge.get('source'))!=knowledge.get('source') or (
            previous.get('dependency_version',knowledge.get('dependency_version'))!=knowledge.get('dependency_version')):
            raise LightGraphError('object_model_supersession_scope_mismatch')
        if previous.get('basis')!='metadata_name_family' or previous.get('status')!='candidate':
            raise LightGraphError('object_model_supersession_target_invalid')
        if not _retains_mapping(previous,incoming):
            raise LightGraphError('object_model_supersession_mapping_loss')
    if inherited and not _retains_mapping(prior,incoming):
        raise LightGraphError('object_model_supersession_mapping_loss')
    incoming['supersedes_object_ids']=list(dict.fromkeys(inherited+requested))
    if len(incoming['supersedes_object_ids'])>32:raise LightGraphError('object_model_supersession_limit')
    incoming.update(status='candidate',basis=basis)
    for field in ('source','dependency_version'):
        if field in knowledge:incoming[field]=deepcopy(knowledge[field])
    value=deepcopy(knowledge)
    value['objects']=[o for o in value.get('objects',[]) if o['object_id']!=model.object_id]
    for obj in value['objects']:
        if obj['object_id'] in requested:
            obj.update(status='superseded',superseded_by=model.object_id)
    value['objects'].append(incoming)
    value['objects_total']=sum(o.get('status')!='superseded' for o in value['objects'])+value.get('objects_omitted',0)
    if 'columns' in value:
        mapped=_mapped_columns(value['objects'])
        value['unassigned_columns']=[c['id'] for c in value['columns'] if c['id'] not in mapped]
    return value


def _current_table_ids(packet):
    scope=packet.get('scope') or {}
    table=scope.get('table')
    if not table:
        return set()
    schema=scope.get('schema', scope.get('schema_name', 'public'))
    source=scope.get('source_id')
    names={table, f'{schema}.{table}'}
    if source:
        names.update({f'{source}:{schema}.{table}', f'{source}.{schema}.{table}'})
    return names


def object_model_reference_slots(model,packet):
    """Safe field-level feedback uses issued references, never rejected values."""
    from .reference_validation import ReferenceSlot,reference_registry
    registry=reference_registry(packet);slots=[]
    if model.table_id:
        names=_current_table_ids(packet)
        if names and model.table_id not in names:
            slots.append(ReferenceSlot(0,('table_id',),'tables','object_model_table_mismatch',names))
            return slots
    def check(path,value,category):
        if value not in registry[category]:
            code=('object_model_cross_table_reference' if category=='columns'
                  else 'dangling_evidence_reference')
            slots.append(ReferenceSlot(0,path,category,code,registry[category]))
    for field in ('key_columns','label_columns','english_name_columns','alias_columns','namespace_columns','evidence_refs'):
        for index,value in enumerate(getattr(model,field)):
            check((field,index),value,'evidence' if field=='evidence_refs' else 'columns')
    for index,position in enumerate(model.positions):
        check(('positions',index,'code'),position.code,'columns')
        if position.label:check(('positions',index,'label'),position.label,'columns')
        for offset,value in enumerate(position.aliases):check(('positions',index,'aliases',offset),value,'columns')
    return slots


def validate_object_model(raw, packet):
    """Validate issued locators, not semantic truth, before retaining a model."""
    model=raw if isinstance(raw,ObjectModelDraft) else ObjectModelDraft.model_validate(raw)
    slots=object_model_reference_slots(model,packet)
    if slots:raise LightGraphError(slots[0].code)
    return model


def hierarchy_field_models(objects):
    """Eligible remembered mappings; metadata alone supplies no hierarchy semantics."""
    return [obj for obj in objects if obj.get('status') not in
        {'superseded','conflict','published','local_validated'}
        and obj.get('basis')=='agent_field_model'
        and len(obj.get('positions') or [])>=2
        and all(item.get('code') and item.get('label') for item in obj['positions'])]


def proposals_from_field_models(objects, definition_refs=()):
    """Legacy callers cannot turn field roles into invented identity/parent policy.

    Use submit_field_model_hierarchy to supply explicit business choices while
    reusing the exact remembered columns. All proposals still require review.
    """
    return []


def _source(table):
    return SourceRef(source_id=table.source_id,schema=table.schema_name,table=table.name)


def _authorized_table(table, goal):
    from .engine import SENSITIVE
    from .goal import column_is_excluded, column_is_evidence_only
    result=table.model_copy(deep=True)
    result.columns=[c for c in result.columns if not SENSITIVE.search(c.name)
        and not column_is_excluded(goal,table,c.name) and not column_is_evidence_only(goal,table,c.name)]
    names=set(result.column_names)
    result.unique_keys=[key for key in result.unique_keys if set(key)<=names]
    result.foreign_keys=[key for key in result.foreign_keys if set(key.columns)<=names]
    return result


def seed_knowledge(table, goal=None, *, dependency_version=''):
    """Name families nominate field groups; no heuristic confirms an identity."""
    from .engine import _column_id, _groups, LEVEL
    goal=goal or ExtractionGoal()
    table=_authorized_table(table,goal)
    groups,namespaces,_,excluded=_groups(table,goal)
    ids={c.name:_column_id(table,c.name) for c in table.columns}
    objects=[]
    for prefix,group in sorted(groups.items()):
        if not group['code'] or not group['label']:continue
        aliases=group['aliases']
        english=[c for c in aliases if re.search(r'(?:_en_name|_english_name|英文名)$',c,re.I)]
        match=LEVEL.search(group['code'])
        positions=([{'position':int(match[2]),'code':ids[group['code']],
            'label':ids[group['label']],'aliases':[ids[c] for c in aliases]}]
            if match and int(match[2])<=64 else [])
        value={'key_columns':[ids[group['code']]],'label_columns':[ids[group['label']]],
            'english_name_columns':[ids[c] for c in english],
            'alias_columns':[ids[c] for c in aliases if c not in english],
            'namespace_columns':[ids[c] for c in namespaces], 'positions':positions,
            'evidence_refs':[], 'status':'candidate','basis':'metadata_name_family'}
        value['object_id']='object_'+digest(_source(table).model_dump(mode='json',by_alias=True),value)[:24]
        value['source']=_source(table).model_dump(mode='json',by_alias=True)
        value['dependency_version']=dependency_version
        objects.append(value)
    used=_mapped_columns(objects)
    return {'version':'schema_knowledge/1','source':_source(table).model_dump(mode='json',by_alias=True),
        'dependency_version':dependency_version,'columns':[{'id':ids[c.name],'name':c.name,
            'type':c.data_type} for c in table.columns], 'objects':objects,
        'candidate_keys':[[ids[c] for c in key] for key in table.unique_keys],
        'unassigned_columns':[ids[c.name] for c in table.columns if ids[c.name] not in used],
        'document_refs':[], 'counts':{},
        'authority':'Remembered field mappings guide exploration; only independent semantic review and full execution validate rules.'}


class SchemaKnowledgeLedger:
    """Append immutable mapping revisions; dependency changes retire old views."""

    def __init__(self, store):
        self.store,self.connection=store,store.connection
        self.tables={};self.versions={};self.goal=ExtractionGoal()
        self.connection.execute('''CREATE TABLE IF NOT EXISTS schema_knowledge (
            source_ref TEXT NOT NULL, dependency_version TEXT NOT NULL, revision INTEGER NOT NULL,
            payload TEXT NOT NULL, state TEXT NOT NULL,
            PRIMARY KEY(source_ref,dependency_version,revision))''')

    def prepare(self,tables,goal,corpus):
        from .goal import source_in_goal
        self.goal=goal;self.tables={};self.versions={}
        with self.store._transaction():
            self.connection.execute("UPDATE schema_knowledge SET state='stale'")
        for original in tables:
            if not source_in_goal(goal,original):continue
            table=_authorized_table(original,goal);source=canonical(_source(table))
            version=digest('schema_knowledge/1',table.model_dump(mode='json',by_alias=True),
                context_dependency_fingerprint(corpus,[table],goal))
            self.tables[source]=table;self.versions[source]=version
            row=self.connection.execute('SELECT payload FROM schema_knowledge WHERE source_ref=? '
                'AND dependency_version=? ORDER BY revision DESC LIMIT 1',(source,version)).fetchone()
            with self.store._transaction():
                self.connection.execute("UPDATE schema_knowledge SET state='stale' WHERE source_ref=? AND dependency_version!=?",(source,version))
                # Unchanged dependencies can reuse the exact previous knowledge.
                if row:self.connection.execute("UPDATE schema_knowledge SET state='current' WHERE source_ref=? AND dependency_version=?",(source,version))
                else:self._append(source,seed_knowledge(table,goal,dependency_version=version))

    def _append(self,source,payload):
        version=self.versions[source]
        mapped=_mapped_columns(payload['objects'])
        payload['unassigned_columns']=[c['id'] for c in payload['columns'] if c['id'] not in mapped]
        row=self.connection.execute('SELECT revision,payload FROM schema_knowledge WHERE source_ref=? '
            'AND dependency_version=? ORDER BY revision DESC LIMIT 1',(source,version)).fetchone()
        encoded=canonical(payload)
        if row and row['payload']==encoded:return
        self.connection.execute('INSERT INTO schema_knowledge VALUES(?,?,?,?,?)',
            (source,version,row['revision']+1 if row else 1,encoded,'current'))

    def _load(self,table):
        source=canonical(_source(table));version=self.versions.get(source)
        if version is None:raise LightGraphError('schema_knowledge_source_not_prepared')
        row=self.connection.execute('SELECT payload FROM schema_knowledge WHERE source_ref=? AND '
            'dependency_version=? ORDER BY revision DESC LIMIT 1',(source,version)).fetchone()
        return json.loads(row['payload'])

    def current_objects(self,table):
        """Active field models, including published ones and excluding retired seeds."""
        return [obj for obj in self._load(table).get('objects',[]) if obj.get('status')!='superseded']

    def field_model_guards(self,table):
        value=self._load(table)
        names={c['id']:c['name'] for c in value['columns']}
        guards=value.get('field_model_guards',{})
        return tuple({names[ref] for ref in guards.get(key,[]) if ref in names}
            for key in ('projection','identity'))

    def record_field_model_guards(self,table,projection=(),identity=()):
        """Keep only authorized column IDs; proposal-local display roles are not prohibitions."""
        source=canonical(_source(table));value=self._load(table)
        ids={c['name']:c['id'] for c in value['columns']}
        guards=value.setdefault('field_model_guards',{})
        for key,names in (('projection',projection),('identity',identity)):
            guards[key]=sorted(set(guards.get(key,[]))|{ids[n] for n in names if n in ids})
        with self.store._transaction():self._append(source,value)

    def set_field_model_work(self,table,state):
        if state not in {'pending','review_refuted','compiled'}:
            raise ValueError('invalid_field_model_work_state')
        value=self._load(table)
        # A timed-out attempt cannot erase an independent negative verdict.
        if value.get('field_model_work')=='review_refuted' and state=='pending':return
        value['field_model_work']=state
        with self.store._transaction():self._append(canonical(_source(table)),value)

    def context(self,table,max_objects=16):
        value=current_object_knowledge(self._load(table))
        value['objects']=sorted(value['objects'],key=lambda o:(
            {'published':0,'local_validated':1,'candidate':2,'conflict':3}[o['status']],
            o['basis']=='metadata_name_family',o['object_id']))
        value['objects_total']=len(value['objects']);value['objects']=value['objects'][:max_objects]
        value['objects_omitted']=value['objects_total']-len(value['objects'])
        # The task's metadata directory is already pinned in every prompt.
        # Do not duplicate potentially hundreds of column descriptors here.
        value.pop('columns',None)
        value['column_directory_ref']='metadata_digest.columns'
        return value

    def record_models(self,table,models,packet):
        source=canonical(_source(table))
        if source not in self.versions:return
        from .engine import _definition_document
        value=self._load(table);allowed={c['id'] for c in value['columns']}
        documents={d['doc_id']+'@'+d['revision']+':'+d['section_id']
            for d in packet.get('document_snippets',[])+packet.get('document_refs',[])
            if all(d.get(k) for k in ('doc_id','revision','section_id')) and _definition_document(d, table)}
        value['document_refs']=sorted(set(value['document_refs'])|documents)
        for probe in packet.get('probe_digest',[]):
            if probe.get('operator')!='profile_columns':continue
            for ref,summary in probe.get('aggregates',{}).items():
                if ref not in allowed or not isinstance(summary,dict):continue
                counts={k:v for k,v in summary.items() if k in {'rows','non_null','distinct_exact',
                    'nulls','empty_str','min_len','max_len'} and type(v) is int and v>=0}
                if counts:value.setdefault('column_counts',{})[ref]={
                    'counts':counts,'basis':'previous_profile','requires_revalidation':True}
        with self.store._transaction():
            for raw in models:
                model=validate_object_model(raw,packet)
                if not _field_ids(model)<=allowed:continue
                item=model.model_dump(mode='json');local_id=item.pop('object_id')
                item['evidence_refs']=sorted(set(item['evidence_refs']) & (documents|allowed))
                prior=next((o for o in value['objects'] if o['object_id']==local_id),None)
                if prior and (prior.get('basis')=='executed_rule' or prior.get('status') in {'local_validated','published'}):
                    raise LightGraphError('object_model_verified_revision_required')
                item['object_id']=(local_id if prior else
                    'object_'+digest(source,'agent_field_model',local_id)[:24])
                value=apply_object_model(value,item,basis='agent_field_model')
                # Every accepted revision is immutable, including two edits in
                # one conversation; only the latest map is injected next time.
                retired=[o for o in value['objects'] if o['status']=='superseded']
                active=[o for o in value['objects'] if o['status']!='superseded']
                value['objects']=sorted(active,key=lambda o:(o['status']=='candidate',
                    o['basis']=='metadata_name_family'))[:128]+retired
                self._append(source,value)
            self._append(source,value)

    def record_rule(self,rule,*,status='local_validated'):
        if status not in {'local_validated','published'}:raise ValueError('knowledge_status_invalid')
        source=canonical(rule.source)
        if source not in self.tables:return
        table=self.tables[source];value=self._load(table)
        previous=[o for o in value['objects'] if o.get('rule_id')==rule.rule_id]
        if any(o.get('rule_revision',0)>rule.revision for o in previous):return
        if status=='local_validated' and any(o['status']=='published' and o.get('rule_revision')==rule.revision for o in previous):
            return
        value['objects']=[o for o in value['objects'] if o.get('rule_id')!=rule.rule_id]
        from .engine import _column_id
        ids={c.name:_column_id(table,c.name) for c in table.columns}
        def refs(names):return [ids[name] for name in names if name in ids]
        positions=[];keys=list(rule.identity.key_columns);labels=[];english=[];aliases=[]
        for name,column in rule.attributes.single_valued.items():
            if name=='label':labels.append(column)
            elif name=='english_name':english.append(column)
            elif name=='alias':aliases.append(column)
        for name,column in rule.attributes.multi_valued.items():
            if name=='english_name':english.append(column)
            elif name=='alias':aliases.append(column)
        for name,columns in getattr(rule.attributes,'name_columns',{}).items():
            (english if name=='english_name' else aliases).extend(columns)
        if rule.hierarchy and rule.hierarchy.levels:
            keys=[]
            for level in rule.hierarchy.levels:
                if level.code not in ids:continue
                keys.append(level.code)
                if level.label:labels.append(level.label)
                for column in level.aliases:
                    (english if re.search(r'(?:_en_name|_english_name|英文名)$',column,re.I) else aliases).append(column)
                positions.append({'position':level.level,'code':ids[level.code],
                    'label':ids.get(level.label),'aliases':refs(level.aliases)})
        models=[{'key_columns':refs(keys),'label_columns':refs(labels),'english_name_columns':refs(english),
            'alias_columns':refs(aliases),'namespace_columns':refs(rule.scope.namespaces),'positions':positions,
            'entity_kind':rule.identity.entity_kind,'identity_domain':rule.identity.identity_domain,
            'identity_scope':rule.identity.identity_scope}]
        if rule.concept_mapping:
            models=[{'key_columns':refs([c.key_column or c.column]),'label_columns':refs([c.column]),
                'english_name_columns':[],'alias_columns':[], 'namespace_columns':refs(
                    rule.scope.namespaces if c.namespace_columns is None else c.namespace_columns),
                'positions':[],'entity_kind':c.entity_kind,'identity_domain':c.identity_domain or '',
                'identity_scope':'component_declared'} for c in rule.concept_mapping.components]
        with self.store._transaction():
            for index,item in enumerate(models):
                if not item['key_columns']:continue
                item.update(object_id='object_'+digest(source,rule.rule_id,index)[:24],status=status,
                    basis='executed_rule',rule_id=rule.rule_id,rule_revision=rule.revision,
                    evidence_refs=list(rule.dependencies.documents))
                value['objects']=[o for o in value['objects'] if o['object_id']!=item['object_id']]+[item]
                positions={(p['position'], p['code']) for p in item['positions']}
                keys=set(item['key_columns'])
                for obj in value['objects']:
                    if obj.get('object_id')==item['object_id'] or obj.get('status')!='candidate':
                        continue
                    if obj.get('basis')!='agent_field_model':
                        continue
                    same_positions=bool(positions) and {(p['position'], p['code'])
                        for p in obj.get('positions') or []}==positions
                    same_keys=not obj.get('positions') and set(obj.get('key_columns') or [])==keys
                    if same_positions or same_keys:
                        obj.update(status='superseded', superseded_by=item['object_id'])
            value['counts']['executed_rules']=len({o['rule_id'] for o in value['objects'] if o.get('rule_id')})
            self._append(source,value)

    def counts(self):
        return {'schema_knowledge_tables':len(self.tables),
            'schema_knowledge_revisions':self.connection.execute('SELECT COUNT(*) FROM schema_knowledge').fetchone()[0]}

    def invalidate_rule(self,rule_id):
        """A failed cross-rule check retires locally validated interpretations."""
        with self.store._transaction():
            for source,table in self.tables.items():
                value=self._load(table);changed=False
                for obj in value['objects']:
                    if obj.get('rule_id')==rule_id and obj['status']!='conflict':
                        obj['status']='conflict';changed=True
                if changed:self._append(source,value)


def is_foreign_identity_copy(rule, rules):
    """A fact-table code column is not a second entity when another table already labels that domain."""
    if rule.kind!='column_group_entity' or rule.attributes.single_valued.get('label'):
        return False
    domain=rule.identity.identity_domain or ''
    keys=set(rule.identity.key_columns)
    if not domain or not keys:
        return False
    for other in rules:
        if other.rule_id==rule.rule_id or other.source==rule.source or other.kind=='reference':
            continue
        if (other.identity.identity_domain or '')!=domain or other.identity.entity_kind!=rule.identity.entity_kind:
            continue
        if keys<=set(other.identity.key_columns) and other.attributes.single_valued.get('label'):
            return True
    return False


def drop_foreign_identity_copies(rules):
    """Return rule_id -> table for copies that must stay available to relation discovery."""
    return {rule.rule_id: rule.source.table for rule in rules if is_foreign_identity_copy(rule, rules)}


def project_retained_labels(rules, knowledge):
    """Fill one unambiguous retained name column before a rule can publish without a label."""
    if knowledge is None:
        return 0
    from .engine import _column_id
    projected=0
    for rule in rules:
        if rule.kind!='column_group_entity' or rule.attributes.single_valued.get('label'):
            continue
        if is_foreign_identity_copy(rule, rules):
            continue
        source=canonical(rule.source)
        if source not in knowledge.tables:
            continue
        table=knowledge.tables[source]
        value=knowledge._load(table)
        ids={c.name:_column_id(table,c.name) for c in table.columns}
        reverse={column_id:name for name,column_id in ids.items()}
        key_ids={ids[name] for name in rule.identity.key_columns if name in ids}
        namespace_ids={ids[name] for name in rule.scope.namespaces if name in ids}
        excluded={item.column for item in rule.attributes.excluded}
        found=[]
        for obj in value.get('objects',[]):
            if obj.get('status') in ('superseded','conflict'):
                continue
            if key_ids-set(namespace_ids)!=set(obj.get('key_columns') or [])-namespace_ids:
                continue
            if namespace_ids!=set(obj.get('namespace_columns') or []):continue
            for ref in obj.get('label_columns') or []:
                name=reverse.get(ref)
                if name and name not in excluded and name not in rule.identity.key_columns and name not in found:
                    found.append(name)
        if len(found)!=1:
            continue
        rule.attributes=rule.attributes.model_copy(update={
            'single_valued':{**rule.attributes.single_valued,'label':found[0]}})
        projected+=1
    return projected


def relation_candidates(tables, knowledge=None, *, goal=None, max_candidates=32):
    """Index matching metadata/validated roles without an all-column cross join.

    No row values or hashes of row values persist here. Candidate pairs must be
    checked with full reference/FD probes and independently interpreted.
    """
    from .engine import _column_id
    if not isinstance(max_candidates,int) or isinstance(max_candidates,bool) or max_candidates<0:
        raise ValueError('invalid_relation_candidate_limit')
    tables=([knowledge.tables[canonical(_source(t))] for t in tables
             if canonical(_source(t)) in knowledge.tables] if knowledge else
            [_authorized_table(t,goal or ExtractionGoal()) for t in tables])
    buckets=defaultdict(dict);declared=[];by_source={(t.source_id,t.qualified_name):t for t in tables}
    def endpoint(table,names):
        return {'source':_source(table).model_dump(mode='json',by_alias=True),
            'column_ids':[_column_id(table,name) for name in names],'columns':list(names)}
    for table in tables:
        state=knowledge._load(table) if knowledge else seed_knowledge(table,goal)
        columns={c['id']:c for c in state['columns']}
        for obj in state['objects']:
            if obj['status'] in {'conflict','superseded'}:continue
            for role,field in [('identity_code','key_columns'),('label','label_columns'),
                               ('english_name','english_name_columns'),('alias','alias_columns')]:
                references=list(obj.get(field,[]))
                if field in {'key_columns','label_columns'}:
                    references.extend(p.get('code' if field=='key_columns' else 'label')
                        for p in obj.get('positions',[]))
                elif field=='alias_columns':
                    references.extend(ref for p in obj.get('positions',[]) for ref in p.get('aliases',[])
                        if ref not in obj.get('english_name_columns',[]))
                for ref in dict.fromkeys(references):
                    if ref not in columns:continue
                    col=columns[ref];item=endpoint(table,[col['name']])
                    family=('number' if re.search(r'int|numeric|decimal|float|double|real',col['type'],re.I) else
                            'text' if re.search(r'text|char|string',col['type'],re.I) else col['type'].lower())
                    buckets[('same_column_name',role,col['name'].casefold(),family)][canonical(item)]=item
                    if obj['status'] in {'local_validated','published'} and obj.get('entity_kind'):
                        buckets[('same_validated_role',role,obj['entity_kind'],family)][canonical(item)]=item
                        if obj.get('identity_domain'):
                            buckets[('same_identity_domain',role,obj['identity_domain'],
                                obj['entity_kind'],family)][canonical(item)]=item
        for fk in table.foreign_keys:
            target=by_source.get((table.source_id,fk.target_schema+'.'+fk.target_table))
            if (not target or len(fk.columns)!=len(fk.target_columns)
                or not set(fk.columns)<=set(table.column_names)
                or not set(fk.target_columns)<=set(target.column_names)):continue
            declared.append({'left':endpoint(table,fk.columns),'right':endpoint(target,fk.target_columns),
                'basis':'declared_foreign_key','key_arity':len(fk.columns)})
    result=[];seen=set();eligible_total=len(declared)
    def add(item):
        key=canonical(sorted([item['left'],item['right']],key=canonical))
        if key in seen:return
        seen.add(key)
        if len(result)<max_candidates:result.append({**item,'status':'candidate',
            'validation':'check_reference_complete_projection_then_semantic_review'})
    for item in declared:add(item)
    for bucket,entries in sorted(buckets.items()):
        groups=defaultdict(list)
        for item in entries.values():groups[canonical(item['source'])].append(item)
        count=sum(len(group) for group in groups.values())
        eligible_total+=count*(count-1)//2-sum(len(group)*(len(group)-1)//2 for group in groups.values())
        if len(result)>=max_candidates:continue
        # Enumerate only between source groups; one large source never triggers
        # a quadratic scan that can produce no cross-table candidate.
        for left_group,right_group in combinations(groups.values(),2):
            for left,right in product(left_group,right_group):
                add({'left':left,'right':right,'basis':bucket[0],'role':bucket[1]})
                if len(result)>=max_candidates:break
            if len(result)>=max_candidates:break
    return {'items':result,'returned':len(result),'candidate_occurrences':eligible_total,
        'omitted_occurrences':max(0,eligible_total-len(result)),
        'counting':'Occurrences across evidence buckets can repeat one pair; count is not unique relationships.',
        'value_overlap_checked':False,'meaning':'Metadata nominates candidates, never confirms business identity or a relation.'}
