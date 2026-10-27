"""Explicit table/column and semantic-column graph, independent of row facts."""
from .contracts import GraphFact,SupportSpec,PartitionCompletion,digest


def metadata_id(domain,source,*parts):
    return 'meta:'+digest(domain,source,*parts)[:24]


def publish_metadata(store,adapter,domain='default'):
    tables=adapter.describe()
    rid='catalog:'+tables[0].source_id if tables else 'catalog:empty'
    version={'schema':digest([t.model_dump(mode='json',by_alias=True) for t in tables]),
             'projection':'metadata_graph/2'}
    if store.current_versions(rid)==version:return len(tables)
    pub=store.begin_publication(rid,1,'all',version)
    excel=adapter.capabilities().product=='excel_memory'
    def emit(kind,payload,binding):
        support=SupportSpec(origin='catalog',source_binding=binding,scope_ref='public',partition_id='all',rule_id=rid,rule_revision=1)
        store.stage_fact(pub,GraphFact(isolation_domain=domain,fact_kind=kind,payload=payload),support)
    def node(nid,kind,label):
        emit('NodeExistence',{'entity_id':nid,'kind':kind,'layer':'metadata'},nid)
        emit('PropertyValue',{'entity_id':nid,'name':'label','value':label},nid)
    available={(t.qualified_name,c.name) for t in tables for c in t.columns}
    try:
        for table in tables:
            db=metadata_id(domain,table.source_id)
            sch=metadata_id(domain,table.source_id,table.schema_name)
            tid=metadata_id(domain,table.source_id,table.qualified_name)
            node(db,'Workbook' if excel else 'Database',table.source_id)
            node(tid,'Worksheet' if excel else 'Table',table.name)
            if table.comment:
                emit('PropertyValue',{'entity_id':tid,'name':'description','value':table.comment[:2000]},tid)
            if excel:emit('Edge',{'source':db,'relation':'has_sheet','target':tid},tid)
            else:
                node(sch,'Schema',table.schema_name)
                emit('Edge',{'source':db,'relation':'has_schema','target':sch},tid)
                emit('Edge',{'source':sch,'relation':'has_table','target':tid},tid)
            for column in table.columns:
                cid=metadata_id(domain,table.source_id,table.qualified_name,column.name)
                node(cid,'Column',column.name)
                for name,value in [('qualified_name',table.source_id+'.'+table.qualified_name+'.'+column.name),('data_type',column.data_type),('nullable',column.nullable),('primary_key',column.primary_key)]:
                    emit('PropertyValue',{'entity_id':cid,'name':name,'value':value},cid)
                if column.comment:emit('PropertyValue',{'entity_id':cid,'name':'description','value':column.comment[:2000]},cid)
                emit('Edge',{'source':tid,'relation':'sheet_has_column' if excel else 'table_has_column','target':cid},cid)
            constraints=[('unique',str(index),columns,None) for index,columns in enumerate(table.unique_keys)]
            primary=[c.name for c in table.columns if c.primary_key]
            if primary:constraints.append(('primary_key','primary_key',primary,None))
            constraints.extend(('foreign_key',fk.name,fk.columns,fk) for fk in table.foreign_keys)
            for kind,name,columns,fk in constraints:
                constraint=metadata_id(domain,table.source_id,table.qualified_name,'constraint',kind,name)
                node(constraint,'Constraint',name)
                emit('PropertyValue',{'entity_id':constraint,'name':'kind','value':kind},constraint)
                emit('Edge',{'source':tid,'relation':'has_constraint','target':constraint},constraint)
                for position,column in enumerate(columns,1):
                    emit('Edge',{'source':constraint,'relation':'constraint_column',
                        'target':metadata_id(domain,table.source_id,table.qualified_name,column),
                        'qualifiers':{'column_position':position}},constraint)
                if fk is None:continue
                for position,(source,target) in enumerate(zip(fk.columns,fk.target_columns),1):
                    target_table=fk.target_schema+'.'+fk.target_table
                    # The YAML keeps the declaration even if the referenced
                    # object lies outside this authorized graph projection.
                    if (target_table,target) not in available:continue
                    remote=metadata_id(domain,table.source_id,target_table,target)
                    emit('Edge',{'source':constraint,'relation':'constraint_target','target':remote,
                        'qualifiers':{'column_position':position}},constraint)
                    emit('Edge',{'source':metadata_id(domain,table.source_id,table.qualified_name,source),
                        'relation':'declared_fk','target':remote,
                        'qualifiers':{'relation_group':constraint,'column_position':position}},tid)
        store.publish_partition(PartitionCompletion(publication_id=pub,complete=True,consistent=True,dependencies_valid=True))
    except BaseException:
        store.abort(pub);raise
    return len(tables)


def stage_rule_metadata(store,publication_id,rule,allrules,domain,entities=()):
    """Publish validated column groups/roles alongside the rule's business facts.

    Structural references are not labelled business equivalence. The business
    relation remains separate and has already passed the rule's own evidence gate.
    """
    binding='rulemeta:'+digest(domain,rule.rule_id)[:24]
    partition_id=store._publication(publication_id)['partition_id']
    support=SupportSpec(origin='catalog',source_binding=binding,scope_ref='public',partition_id=partition_id,
        rule_id=rule.rule_id,rule_revision=rule.revision,validation_ref=rule.validation_ref)
    def emit(kind,payload):store.stage_fact(publication_id,GraphFact(isolation_domain=domain,fact_kind=kind,payload=payload),support)
    def column(r,c):return metadata_id(domain,r.source.source_id,r.source.schema_name+'.'+r.source.table,c)
    group=metadata_id(domain,rule.source.source_id,'group',rule.rule_id)
    emit('NodeExistence',{'entity_id':group,'kind':'ColumnGroup','layer':'metadata'})
    emit('PropertyValue',{'entity_id':group,'name':'label','value':rule.source.table+' / '+rule.rule_id})
    roles={**rule.attributes.single_valued,**rule.attributes.multi_valued}
    for c in rule.identity.key_columns:roles['identity:'+c]=c
    if rule.hierarchy:
        for lv in rule.hierarchy.levels:
            roles['level_key:'+str(lv.level)]=lv.code
            if lv.label:roles['level_label:'+str(lv.level)]=lv.label
    for role,col in roles.items():
        cid=column(rule,col);slot=metadata_id(domain,rule.source.source_id,'slot',rule.rule_id,role)
        emit('NodeExistence',{'entity_id':slot,'kind':'SemanticSlot','layer':'metadata'})
        emit('PropertyValue',{'entity_id':slot,'name':'label','value':role})
        emit('Edge',{'source':cid,'relation':'member_of_group','target':group})
        emit('Edge',{'source':cid,'relation':'expresses_role','target':slot})
    import json
    entities=[json.loads(f['payload'])['entity_id'] for f in store._fact_rows([publication_id]) if f['fact_kind']=='NodeExistence' and json.loads(f['payload']).get('layer')=='business']
    for entity in sorted(set(entities)) if rule.identity.entity_kind=='BusinessConcept' else ():
        emit('Edge',{'source':group,'relation':'maps_to_concept','target':entity})
    if rule.relation:
        from .rules.endpoints import endpoint_columns
        endpoint_rules={r.rule_id:r for r in allrules}
        for rid,cols,level in [(rule.relation.source_rule_id,rule.relation.source_columns,rule.relation.source_level),
                              (rule.relation.target_rule_id,rule.relation.target_columns,rule.relation.target_level)]:
            target=endpoint_rules.get(rid)
            if not target:continue
            for c,key in zip(cols,endpoint_columns(target,level)):
                emit('Edge',{'source':column(rule,c),'relation':'verified_reference','target':column(target,key)})
