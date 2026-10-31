"""Check a proposal against explicitly remembered field mappings, not truth.

Name-family guesses never veto a proposal. A mapping conflict is repairable by
revising either the proposal or the remembered candidate; semantic review still
decides whether the resulting interpretation is supported.
"""
from __future__ import annotations


def object_mapping_diagnostics(proposal, packet, *, hierarchy_plan=False):
    columns={c['id'] for c in packet.get('metadata_digest',{}).get('columns',[])}
    mappings={}
    for obj in packet.get('schema_knowledge',{}).get('objects',[]):
        declared=obj.get('basis') in {'agent_field_mapping','agent_field_model'}
        validated=(obj.get('basis')=='executed_rule' and obj.get('status') in {'local_validated','published'})
        if obj.get('status')=='conflict' or not (declared or validated):continue
        priority=3 if obj.get('basis')=='agent_field_mapping' else 2 if declared else 1
        def remember(keys,scope,labels):
            target=(keys,scope)
            # A deliberate field-model revision can retract a candidate name
            # slot. The independent compiler/reviewer still validates rules.
            if keys and keys|scope|labels<=columns and priority>=mappings.get(target,(0,set()))[0]:
                mappings[target]=(priority,labels)
        scope=frozenset(obj.get('namespace_columns',[]))
        keys=frozenset(obj.get('key_columns',[]))-scope
        labels=set(obj.get('label_columns',[]))-keys-scope
        remember(keys,scope,labels)
        for position in obj.get('positions',[]):
            key=position.get('code');label=position.get('label')
            if key in columns and scope<=columns:
                remember(frozenset({key}),scope,{label} if label in columns and key!=label else set())
    if not mappings:return []
    diagnostics=[]
    def check(keys, scope, label, field):
        scope=frozenset(scope);keys=frozenset(keys)-scope
        if label not in keys:return
        expected=sorted(mappings.get((keys,scope),(0,set()))[1])
        if expected:
            diagnostics.append({'code':'object_model_mapping_conflict','field':field,
                'constraint':'declared_code_name_mapping','category':'columns',
                'allowed':expected,'guidance':'This object already has a distinct primary-name mapping. '
                    'Use its issued name column, or explicitly revise declare_object_model first if '
                    'the remembered candidate is wrong. Use replace_draft_id for an existing draft. '
                    'This feedback does not confirm semantic identity.'})
    claim=proposal.claim
    if proposal.kind=='column_group':
        keys=[m.column_id for m in claim.members if m.in_identity and (m.role=='identity_code'
            or (m.role=='semantic_attribute' and m.property_name=='business_code'))]
        scope=[m.column_id for m in claim.members if m.role=='namespace']
        for index,member in enumerate(claim.members):
            if not member.exclude_from_projection and (member.role=='label' or
                    (member.role=='semantic_attribute' and member.property_name=='label')):
                check(keys,scope,member.column_id,['claim','members',index,'column_id'])
    elif proposal.kind=='entity_key':
        # Match the compiler's existing implicit-property contract. Column-name
        # patterns never supply remembered evidence for this check.
        from .engine import SEMANTIC
        names={c['id']:c.get('name','') for c in packet.get('metadata_digest',{}).get('columns',[])}
        for field,default in [('single_valued','label'),('multi_valued','alias')]:
            for index,column in enumerate(getattr(claim.attributes,field)):
                implicit=SEMANTIC.get(names.get(column,''),default) if field=='single_valued' else default
                if claim.attributes.property_names.get(column,implicit)=='label':
                    check(claim.key_columns,claim.namespace_columns,column,['claim','attributes',field,index])
    elif proposal.kind=='concept_mapping':
        for index,component in enumerate(claim.components):
            scope=claim.namespace_columns if component.namespace_columns is None else component.namespace_columns
            check([component.key_column or component.column],scope,component.column,
                  ['claim','components',index,'column'])
    elif proposal.kind=='wide_hierarchy':
        for index,level in enumerate(claim.levels):
            field=['positions',index,'label'] if hierarchy_plan else ['claim','levels',index,'label']
            check([level.key],claim.namespace_columns,level.label,field)
    elif proposal.kind=='long_hierarchy':
        check([claim.key_column],claim.namespace_columns,claim.label_column,['claim','label_column'])
    return diagnostics[:8]
