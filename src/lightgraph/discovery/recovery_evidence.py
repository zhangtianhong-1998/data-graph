"""Recheck a retained grain rejection without treating old probe IDs as evidence.

Legacy probes have no replayable query. For a provable composite-to-position
change only, current full-column aggregates support a NEW review of that change.
The historical observation is neither replayed nor claimed equivalent.
"""
from lightgraph.errors import LightGraphError


def reserved_evidence_probe_ids(packet):
    """Every fresh executor must keep historical and current probe IDs apart."""
    refs={p['probe_id'] for p in packet.get('probe_digest',[])}
    def visit(value):
        if isinstance(value,dict):
            for key,child in value.items():
                if key in {'blocking_evidence_refs','required_evidence_refs','historical_probe_refs'} and isinstance(child,list):
                    refs.update(ref for ref in child if isinstance(ref,str) and ref.startswith('probe_'))
                elif isinstance(child,(dict,list)):visit(child)
        elif isinstance(value,list):
            for child in value:visit(child)
    visit(packet.get('iteration_context',{}));visit(packet.get('semantic_revision_guard',{}))
    return refs


def prepare_grain_recovery(packet, table, objects, candidates, tools, *, documents=None):
    from .agent import _record_probe
    from .engine import _definition_document
    columns={c['id']:c['name'] for c in packet['metadata_digest']['columns']}
    material=list(documents if documents is not None else packet['document_snippets'])
    docs={d['doc_id']+'@'+d['revision']+':'+d['section_id'] for d in material}
    definitions={d['doc_id']+'@'+d['revision']+':'+d['section_id'] for d in material
        if _definition_document(d,table)}
    historical={ref for c in candidates for ref in c.get('blocking_evidence_refs',[])+c.get('required_evidence_refs',[]) if ref.startswith('probe_')}
    # An already reused ordinal is ambiguous. Do not expose it as issued
    # historical evidence or invent a renaming of observations.
    if historical & {p['probe_id'] for p in packet.get('probe_digest',[])}:
        raise LightGraphError('semantic_recovery_evidence_required')
    # Neither reference identity nor aggregate equality links sessions.
    tools.reserve_probe_ids(historical|reserved_evidence_probe_ids(packet))
    guard={'blocking_evidence_refs':[], 'conditions':[], 'recovery_obligations':[]}
    rechecks=0
    for candidate in candidates:
        refs=set(candidate.get('blocking_evidence_refs',[])+candidate.get('required_evidence_refs',[]))
        stable=refs-historical
        if not stable <= docs|set(columns):
            raise LightGraphError('semantic_recovery_evidence_required')
        guard['blocking_evidence_refs'].extend(sorted(stable))
        if not refs & historical and all(m.get('entity_kind') is not None for m in candidate.get('mappings',[])):
            continue
        matches=[]
        for obj in objects:
            positions=obj.get('positions') or []
            keys={p['code'] for p in positions}
            namespaces=set(obj.get('namespace_columns') or [])
            for mapping in candidate.get('mappings',[]):
                before={col for f in mapping.get('fields',[]) if f['role']=='key' for col in f['columns']}-namespaces
                if len(before)>=2 and before<=keys:
                    matches.append((obj,mapping,before))
        if len(matches)!=1:
            raise LightGraphError('semantic_recovery_evidence_required')
        obj,mapping,before=matches[0]
        unknown_kind=mapping.get('entity_kind') is None
        if unknown_kind:
            # Older snapshots removed types absent from that packet's registry.
            # Null records lost knowledge, not a type the replacement must use.
            # Current definitions and an independent bound review must establish it.
            if not definitions:
                raise LightGraphError('semantic_recovery_evidence_required')
            stable|=definitions
        positions=obj['positions'];scope=obj.get('namespace_columns') or []
        fields=set(scope)|{p['code'] for p in positions}|{p['label'] for p in positions if p.get('label')}
        fields.update(c for p in positions for c in p.get('aliases',[]))
        if not fields<=set(columns) or not set(candidate.get('column_refs',[]))<=fields or len(fields)>32:
            raise LightGraphError('semantic_recovery_evidence_required')
        # Profile aliases as attributes; multiple English names are not a failed
        # code-to-primary-label dependency. Full execution still validates them.
        checks=[('profile_columns',{'table':table.qualified_name,'columns':[columns[c] for c in sorted(fields)]})]
        checks.extend(('check_fd',{'table':table.qualified_name,
            'keys':[columns[c] for c in dict.fromkeys(scope+[p['code']])],
            'values':[columns[p['label']]]}) for p in positions if p.get('label'))
        current=[];descriptions=[]
        by_name={name:ref for ref,name in columns.items()}
        for name,arguments in checks:
            if rechecks>=32 or len(packet.get('probe_digest',[]))>=96:
                raise LightGraphError('semantic_recovery_evidence_required')
            rechecks+=1
            response=tools.execute(name,arguments)
            if response.get('status')!='ok' or not response.get('coverage',{}).get('result_complete'):
                raise LightGraphError('semantic_recovery_evidence_required')
            _record_probe(packet,response,name)
            current.append(response['probe_id'])
            descriptions.append({'probe_id':response['probe_id'],'operator':name,
                'table':{'source_id':table.source_id,'schema':table.schema_name,'table':table.name},
                **{key:[by_name[column] for column in arguments[key]]
                    for key in ('columns','keys','values') if key in arguments},
                'coverage':'full_authorized_table_aggregate',
                'snapshot_scope':'per_probe_full_rule_validation_still_required'})
        guard['blocking_evidence_refs'].extend(sorted(stable|set(current)))
        guard['recovery_obligations'].append({
            'candidate_id':candidate['candidate_id'], 'prior_failure':'semantic_claim_refuted',
            'prior_review_stage':'entity_granularity','prior_mapping':mapping,
            'historical_probe_refs':sorted(refs&historical),
            'historical_probe_status':'unavailable_not_replayed',
            'recheck_basis':'current_full_column_aggregates_for_structural_alternative',
            'required_evidence_refs':sorted(stable|set(current)),
            'current_rechecks':descriptions,
            'replacement_object_id':obj['object_id'],
            'replacement_entity_kind':mapping.get('entity_kind'),
            'entity_kind_requirement':'confirm_from_current_definition' if unknown_kind else 'preserve_known_kind',
            'entity_kind_definition_refs':sorted(definitions) if unknown_kind else [],
            'replacement_positions':positions,'replacement_namespaces':scope,
        })
    guard['blocking_evidence_refs']=sorted(set(guard['blocking_evidence_refs']))
    return guard


def select_recovery_guard(proposal, guard):
    """A table can contain several independent position models and rejections."""
    from copy import deepcopy
    selected=deepcopy(guard)
    keys={p.key for p in proposal.claim.levels}
    obligations=guard.get('recovery_obligations',[])
    matches=[]
    for obligation in obligations:
        _retained_kind(obligation)
        mapping=obligation['prior_mapping']
        before={col for field in mapping['fields'] if field['role']=='key' for col in field['columns']}
        before-=set(obligation['replacement_namespaces'])
        # Association follows physical keys, not the proposed type or a full
        # subset match. Renaming the kind or dropping a position cannot remove
        # an existing review obligation; shape validation rejects those edits.
        if before & keys:matches.append(obligation)
    selected['recovery_obligations']=deepcopy(matches)
    all_current={ref for o in obligations for ref in o['required_evidence_refs'] if ref.startswith('probe_')}
    applicable={ref for o in matches for ref in o['required_evidence_refs']}
    selected['blocking_evidence_refs']=sorted((set(guard['blocking_evidence_refs'])-all_current)|applicable)
    return selected


def _retained_kind(obligation):
    """Reject corrupt guards separately from missing evidence or a changed claim."""
    required={'prior_mapping','replacement_entity_kind','replacement_positions','replacement_namespaces'}
    if not required<=set(obligation) or not isinstance(obligation['prior_mapping'],dict):
        raise LightGraphError('recovery_state_invalid')
    kind=obligation['prior_mapping'].get('entity_kind')
    if (obligation['replacement_entity_kind']!=kind
            or not isinstance(obligation['replacement_positions'],list)
            or not isinstance(obligation['replacement_namespaces'],list)):
        raise LightGraphError('recovery_state_invalid')
    return kind


def validate_recovery_shape(proposal, guard, *, allowed_entity_kinds=()):
    """The fresh review only applies to the exact retained structural change."""
    for obligation in guard.get('recovery_obligations',[]):
        kind=_retained_kind(obligation)
        claim=proposal.claim
        positions=[{'position':p.position,'code':p.key,'label':p.label,'aliases':p.aliases} for p in claim.levels]
        if (proposal.kind!='wide_hierarchy' or claim.path_semantics!='relative_path'
                or kind is not None and claim.entity_kind_hint!=kind
                or positions!=obligation['replacement_positions']
                or claim.namespace_columns!=obligation['replacement_namespaces']):
            raise LightGraphError('semantic_recovery_shape_changed')
        if kind is None:
            if claim.entity_kind_hint not in allowed_entity_kinds:
                raise LightGraphError('unregistered_node_kind')
            if (obligation.get('entity_kind_requirement')!='confirm_from_current_definition'
                    or not obligation.get('entity_kind_definition_refs')):
                raise LightGraphError('semantic_recovery_evidence_required')


def recovery_rule_matches(rule, obligation, table):
    """Only an independently reviewed exact replacement can close unknown-type work."""
    from .engine import _column_id
    kind=_retained_kind(obligation)
    if (rule.kind!='wide_hierarchy' or rule.identity.level_policy!='relative_path'
            or rule.evidence.semantic_status!='supported'
            or kind is not None and rule.identity.entity_kind!=kind):
        return False
    positions=[{'position':p.level,'code':_column_id(table,p.code),
        'label':_column_id(table,p.label) if p.label else None,
        'aliases':[_column_id(table,c) for c in p.aliases]} for p in rule.hierarchy.levels]
    return (positions==obligation['replacement_positions']
        and [_column_id(table,c) for c in rule.scope.namespaces]==obligation['replacement_namespaces'])
