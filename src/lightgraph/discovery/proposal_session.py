"""Ephemeral, incremental discovery drafts; submission never publishes a rule.

Each local operation validates one typed hypothesis. Source tools still own data
access, and the ordinary compiler retains all semantic and execution gates.
"""
from __future__ import annotations

from copy import deepcopy
from collections import Counter
from typing import Literal

from pydantic import Field, ValidationError

from lightgraph.contracts import StrictModel, digest
from lightgraph.errors import LightGraphError
from lightgraph.semantic_schema import SemanticSchema
from .models import (ProposalBatch, TASK_PROPOSAL_KINDS, ColumnGroupProposal,
    EntityKeyProposal, HierarchyProposal, LongHierarchyProposal, ReferenceProposal,
    DiscriminatorProposal, ExclusionProposal, ConceptMappingProposal)
from .reference_validation import (normalize_column_references, normalize_definition_references,
                                  invalid_reference_slots)
from .validation_feedback import model_diagnostics


class DiscoveryFinish(StrictModel):
    """Small final response; hypotheses have already been submitted as tools."""
    action: Literal['finish_discovery', 'abstain']
    pending: bool = Field(default=False, description=
        'Set true if relevant work remains. False cannot resolve rejected attempts; '
        'resubmit successfully with resolves_rejections or explicitly withdraw their IDs.')
    withdraw_rejections: list[str] = Field(default_factory=list, max_length=16, description=
        'Explicit rejection IDs to withdraw without creating a draft or confirming a fact.')


PROPOSAL_MODELS = {
    'column_group': ColumnGroupProposal, 'entity_key': EntityKeyProposal,
    'wide_hierarchy': HierarchyProposal, 'long_hierarchy': LongHierarchyProposal,
    'reference': ReferenceProposal, 'discriminator': DiscriminatorProposal,
    'exclusion': ExclusionProposal,
    'concept_mapping': ConceptMappingProposal,
}
MAX_SUBMISSION_CALLS = 16


def _compatible_replacement(old, new):
    """A local display repair must not silently replace identity or obligations.

    Structural alternatives still use the independent semantic revision flow.
    Evidence and exclusion commitments accumulate even for a display repair.
    """
    if old.kind!=new.kind or not set(old.evidence_cited)<=set(new.evidence_cited):return False
    before=old.claim.model_dump(mode='json');after=new.claim.model_dump(mode='json')
    for field,value in before.items():
        if field.endswith('_evidence') and isinstance(value,list):
            if not set(value)<=set(after.get(field,[])):return False
            after[field]=value
    if old.kind=='column_group':
        def protected(members):
            return sorted((m['column_id'],m['role'],m['in_identity'],m['exclude_from_projection'],m['property_name'] or '')
                for m in members if m['in_identity'] or m['exclude_from_projection']
                or m['role'] in {'namespace','display_order','audit','fact_value'})
        if protected(before.pop('members'))!=protected(after.pop('members')):return False
    elif old.kind=='entity_key':
        old_attrs=before.pop('attributes');new_attrs=after.pop('attributes')
        if not set(old_attrs['display_only'])<=set(new_attrs['display_only']):return False
    elif old.kind=='wide_hierarchy':
        for claim in (before,after):
            for level in claim['levels']:
                for field in ('label','alias','aliases'):level.pop(field,None)
    elif old.kind=='long_hierarchy':
        for claim in (before,after):
            claim.pop('label_column',None);claim.pop('alias_columns',None)
    elif old.kind=='concept_mapping':
        for claim in (before,after):
            for component in claim['components']:component.pop('column',None)
    else:return False
    return before==after


def _spec(name, description, parameters):
    return {'type': 'function', 'function': {'name': name,
        'description': description, 'parameters': parameters}}


def proposal_tool_specs(task):
    """Expose only this task's kinds; no tool repeats the full batch vocabulary."""
    specs = []
    for kind in sorted(TASK_PROPOSAL_KINDS[task]):
        # Fresh discovery uses business choices rather than redundant internal
        # hierarchy fields. Revision still needs the exact old domain/taxonomy
        # and distinct evidence bindings when repairing an existing rule.
        if kind == 'wide_hierarchy' and task != 'T5_revision':
            continue
        schema = deepcopy(PROPOSAL_MODELS[kind].model_json_schema())
        schema['properties'].pop('kind')
        schema['required'].remove('kind')
        specs.append(_spec('submit_' + kind,
            'Validate and retain one candidate. This does not confirm semantics or publish it. '
            'Use issued references; an error leaves prior drafts intact and does not retain '
            'the rejected input. Correct the diagnosed fields and resubmit, or withdraw '
            'the rejected attempt when finishing.', schema))
    if 'wide_hierarchy' in TASK_PROPOSAL_KINDS[task]:
        from .hierarchy_plan import hierarchy_plan_tool_schema
        specs.insert(0,hierarchy_plan_tool_schema())
    if task in {'T0_discovery','T1_table_discovery','T3_hierarchy','T5_revision'}:
        from .schema_knowledge import ObjectModelDraft
        specs.append(_spec('declare_object_model',
            'Record a candidate table object and its issued code, name, English-name, alias, scope '
            'and position columns before proposing extraction. Reuse object_id to revise the candidate. '
            'This does not validate semantics, replace a rule, or grant publication authority. '
            'Source values and free-text descriptions are forbidden.',ObjectModelDraft.model_json_schema()))
    specs.append(_spec('declare_semantic_schema',
        'Add new vocabulary definitions once. Existing definitions cannot be overwritten.',
        SemanticSchema.model_json_schema()))
    specs.append(_spec('finish_discovery',
        'Finish using all retained drafts. Set pending=true if relevant work remains. '
        'Use withdraw_rejections for explicit rejected-attempt withdrawal; pending=false cannot clear them. '
        'Never resubmit the full batch; finishing does not approve semantics.',
        {'type':'object','properties':{'pending':{'type':'boolean','default':False,
            'description':DiscoveryFinish.model_json_schema()['properties']['pending']['description']},
            'withdraw_rejections':DiscoveryFinish.model_json_schema()['properties']['withdraw_rejections']},
         'additionalProperties':False}))
    for spec in specs:
        if spec['function']['name']=='finish_discovery':continue
        spec['function']['parameters']['properties']['resolves_rejections']={
            'type':'array','items':{'type':'string'},'maxItems':16,'default':[],
            'description':'IDs of rejected attempts corrected by this same tool and same group_id/plan_id. '
                'They resolve only after validation. If feedback says resolution=explicit_withdrawal, '
                'submit the correction without this field, then explicitly withdraw the old rejection ID when finishing.'}
        if spec['function']['name'].startswith('submit_') and spec['function']['name'] not in {
                'submit_reference','submit_exclusion','submit_discriminator'}:
            spec['function']['parameters']['properties']['replace_draft_id']={
                'type':['string','null'],'default':None,
                'description':'Explicitly replace a retained draft from this same tool and group_id/plan_id. '
                    'Keep identity, namespace, topology, exclusions and evidence obligations. '
                    'Use this to correct or enrich display attributes instead of submitting conflicting siblings. '
                    'Invalid replacement leaves the previous draft intact.'}
    return specs


class ProposalSession:
    """One bounded memory-only ledger, cleared when the Agent invocation exits."""

    def __init__(self, packet):
        self.packet = packet
        self.task = packet['task']
        self.drafts = []
        self.draft_origins = []
        self.object_models = []
        self._mapping_rejections = {}
        self._replacement_index = None
        self.schema = SemanticSchema()
        self.finished = False
        self.pending = False
        self.invalid_count = 0
        self.invalid_codes = set()
        self.invalid_code_counts = Counter()
        self.invalid_diagnostics = Counter()
        self.open_rejections = {}
        self._active_operator = None
        self._active_target = None
        self.names = {item['function']['name'] for item in proposal_tool_specs(self.task)}

    def reject(self, code='invalid_proposal_arguments', *, diagnostics=None):
        self.invalid_count += 1
        self.invalid_codes.add(code)
        self.invalid_code_counts[code]+=1
        rejection_id=f'rejection_{self.invalid_count}'
        self.open_rejections[rejection_id]=(self._active_operator,self._active_target)
        for diagnostic in (diagnostics or [{}])[:8]:
            path=diagnostic.get('path',diagnostic.get('field',[]))
            constraint=diagnostic.get('constraint','reference' if diagnostic.get('category') else 'contract')
            # Diagnostics supplied here contain schema fields and offsets only.
            # A submission may have several different reference failures. Do
            # not relabel every field with the submission's first error code.
            field_code=diagnostic.get('code')
            allowed={'unauthorized_column_reference','dangling_evidence_reference',
                'identity_domain_evidence_unresolved','dangling_order_evidence',
                'unverified_entity_reference','unauthorized_selector_reference'}
            self.invalid_diagnostics[(field_code if field_code in allowed else code,tuple(path),constraint)]+=1
        result = {'status':'error', 'code':code,
                  'rejection_id':rejection_id,
                  'resolution':'same_target_submission' if self._active_target is not None else 'explicit_withdrawal',
                  'retained_drafts':len(self.drafts), 'confirmed':False}
        if self._active_operator in self.names:
            result['operator']=self._active_operator
        if diagnostics:
            result['diagnostics'] = diagnostics
        return result

    def execute(self, name, arguments):
        previous=(self._active_operator,self._active_target,self._replacement_index)
        self._active_operator=name
        self._active_target=self._target(name,arguments)
        try:
            if not isinstance(arguments,dict):
                return self.reject('invalid_proposal_arguments',diagnostics=[{'path':[],'constraint':'type'}])
            arguments=dict(arguments)
            replacement=arguments.pop('replace_draft_id',None)
            self._replacement_index=None
            if replacement is not None:
                valid_ids={f'draft_{i+1}':i for i in range(len(self.drafts))}
                if (not isinstance(replacement,str) or replacement not in valid_ids
                        or self._active_target is None
                        or self.draft_origins[valid_ids[replacement]]!=(name,self._active_target)):
                    return self.reject('invalid_draft_replacement')
                self._replacement_index=valid_ids[replacement]
            resolves=arguments.pop('resolves_rejections',[])
            if (not isinstance(resolves,list) or len(resolves)>16 or
                any(not isinstance(r,str) or r not in self.open_rejections or
                    self.open_rejections[r][0]!=name or
                    self._active_target is None or
                    self.open_rejections[r][1]!=self._active_target for r in resolves)):
                return self.reject('invalid_rejection_resolution')
            result=self._execute(name,arguments)
            if result.get('status')=='ok':
                for r in resolves:self.open_rejections.pop(r,None)
            return result
        finally:
            self._active_operator,self._active_target,self._replacement_index=previous

    @staticmethod
    def _target(name, arguments):
        # Only explicit stable proposal IDs establish that a validated retry
        # repaired the rejected candidate. Anonymous attempts require withdrawal;
        # treating None as a wildcard lets an unrelated candidate hide a failure.
        if not isinstance(arguments,dict):return None
        grouped={'submit_column_group','submit_entity_key','submit_wide_hierarchy',
                 'submit_long_hierarchy','submit_concept_mapping'}
        if name=='declare_object_model':
            identifier=arguments.get('object_id')
            return digest(name,identifier) if isinstance(identifier,str) and identifier else None
        if name!='submit_hierarchy_plan' and name not in grouped:return None
        claim=arguments.get('claim',{})
        identifier=arguments.get('plan_id') if name=='submit_hierarchy_plan' else (
            claim.get('group_id') if isinstance(claim,dict) else None)
        return digest(name,identifier) if isinstance(identifier,str) and identifier else None

    def reject_tool_arguments(self, name, arguments, diagnostics):
        previous=(self._active_operator,self._active_target)
        self._active_operator=name;self._active_target=self._target(name,arguments)
        try:return self.reject('invalid_tool_arguments',diagnostics=diagnostics)
        finally:self._active_operator,self._active_target=previous

    def _execute(self, name, arguments):
        if self.finished:
            return self.reject('discovery_session_finished')
        contract = None
        payload = arguments
        try:
            if name == 'finish_discovery':
                contract = DiscoveryFinish
                payload = {'action':name, **arguments}
                command = contract.model_validate(payload)
                conflicts=self._retained_mapping_conflicts()
                if conflicts:
                    return {**conflicts[0][1],'drafts_requiring_revision':[result for _,result in conflicts]}
                if any(r not in self.open_rejections for r in command.withdraw_rejections):
                    return self.reject('unknown_rejection_id')
                for r in command.withdraw_rejections:self.open_rejections.pop(r,None)
                self.pending = command.pending or bool(self.open_rejections)
                self.finished = True
                return {'status':'ok','finished':True,'retained_drafts':len(self.drafts),
                        'confirmed':False,'pending':self.pending,
                        'unresolved_rejections':list(self.open_rejections)}
            if name == 'declare_semantic_schema':
                contract = SemanticSchema
                incoming = contract.model_validate(arguments)
                # Unlike project schema merging, session declarations must not
                # rewrite even descriptions: earlier constraints remain intact.
                for field in ('entity_types','properties','relations'):
                    prior = {item.name:item for item in getattr(self.schema,field)}
                    if any(item.name in prior and item != prior[item.name]
                           for item in getattr(incoming,field)):
                        return self.reject('semantic_definition_conflict')
                self.schema = SemanticSchema.merged(self.schema,incoming)
                return {'status':'ok','schema_declared':True,'confirmed':False}
            if name == 'declare_object_model' and name in self.names:
                from .schema_knowledge import ObjectModelDraft,validate_object_model,object_model_reference_slots
                contract=ObjectModelDraft
                candidate=contract.model_validate(arguments)
                slots=object_model_reference_slots(candidate,self.packet)
                if slots:
                    return self.reject(slots[0].code,diagnostics=[slot.diagnostic(i) for i,slot in enumerate(slots[:8])])
                incoming=validate_object_model(candidate,self.packet).model_dump(mode='json')
                if len(self.object_models)>=32:return self.reject('object_model_limit')
                self.object_models.append(incoming)
                # The formatter shares this packet, so the latest candidate
                # survives source-history eviction while the full history stays
                # available for the workflow's versioned, value-free ledger.
                knowledge=self.packet.setdefault('schema_knowledge',{})
                knowledge['objects']=[o for o in knowledge.get('objects',[])
                    if o.get('object_id')!=incoming['object_id']]+[{**incoming,'status':'candidate',
                        'basis':'agent_field_mapping'}]
                return {'status':'ok','object_model':incoming,'candidate':True,'confirmed':False}
            if name == 'submit_hierarchy_plan':
                from .hierarchy_plan import HierarchyPlan, compile_hierarchy_plan
                contract = HierarchyPlan
                converted = compile_hierarchy_plan(arguments,self.packet,validate_references=False)
                slots=invalid_reference_slots(converted,self.packet)
                if slots:
                    return self.reject(slots[0].code,diagnostics=[slot.diagnostic(i) for i,slot in enumerate(slots[:8])])
                if self._replacement_index is not None and not _compatible_replacement(
                        self.drafts[self._replacement_index],converted.proposals[0]):
                    return self.reject('draft_replacement_changes_obligations')
                # The adapter supplies only a fallback entity description.
                # Reuse issued or explicitly declared vocabulary verbatim;
                # this exception never applies to user declaration changes.
                known = {item.name:item for item in SemanticSchema.model_validate(
                    self.packet.get('semantic_schema',{})).entity_types}
                known.update({item.name:item for item in self.schema.entity_types})
                converted.semantic_schema.entity_types = [known.get(item.name,item)
                    for item in converted.semantic_schema.entity_types]
                previous_schema=self.schema.model_copy(deep=True)
                declared = self.execute('declare_semantic_schema',
                    converted.semantic_schema.model_dump(mode='json'))
                if declared['status'] != 'ok':
                    return declared
                retained=self._retain(converted.proposals[0])
                if retained['status']!='ok':self.schema=previous_schema
                return retained
            kind = name.removeprefix('submit_')
            if kind not in TASK_PROPOSAL_KINDS[self.task]:
                return self.reject('task_kind_forbidden')
            contract = PROPOSAL_MODELS[kind]
            payload = {'kind':kind, **arguments}
            proposal = contract.model_validate(payload)
            candidate = ProposalBatch(task=self.task,proposals=[proposal])
            candidate, _ = normalize_column_references(candidate,self.packet)
            candidate, _ = normalize_definition_references(candidate,self.packet)
            slots=invalid_reference_slots(candidate,self.packet)
            if slots:
                return self.reject(slots[0].code,diagnostics=[slot.diagnostic(i) for i,slot in enumerate(slots[:8])])
            proposal = candidate.proposals[0]
            return self._retain(proposal)
        except ValidationError as exc:
            return self.reject('invalid_proposal_arguments', diagnostics=(
                model_diagnostics(exc, contract.model_json_schema(), payload) if contract else None))
        except LightGraphError as exc:
            return self.reject(exc.code)

    def _retain(self, proposal):
        from .object_mapping_feedback import object_mapping_diagnostics
        diagnostics=object_mapping_diagnostics(proposal,self.packet,
            hierarchy_plan=self._active_operator=='submit_hierarchy_plan')
        if diagnostics:
            result=self.reject('object_model_mapping_conflict',diagnostics=diagnostics)
            target=getattr(proposal.claim,'group_id',None)
            for index,old in enumerate(self.drafts):
                if target and old.kind==proposal.kind and getattr(old.claim,'group_id',None)==target:
                    result['replace_draft_id']=f'draft_{index+1}'
                    result['draft_operator']=self.draft_origins[index][0]
                    break
            return result
        index=self._replacement_index
        if index is not None:
            if not _compatible_replacement(self.drafts[index],proposal):
                return self.reject('draft_replacement_changes_obligations')
            self.drafts[index]=proposal
            self._mapping_rejections.pop(index,None)
            return {'status':'ok','draft_id':f'draft_{index+1}','replaced':True,'confirmed':False}
        for index, old in enumerate(self.drafts):
            if old == proposal:
                return {'status':'ok','draft_id':f'draft_{index+1}',
                        'duplicate':True,'confirmed':False}
            target=getattr(proposal.claim,'group_id',None)
            if target and old.kind==proposal.kind and getattr(old.claim,'group_id',None)==target:
                result=self.reject('draft_replacement_required')
                result['replace_draft_id']=f'draft_{index+1}'
                result['draft_operator']=self.draft_origins[index][0]
                return result
        if len(self.drafts) >= 6:return self.reject('proposal_draft_limit')
        self.drafts.append(proposal)
        self.draft_origins.append((self._active_operator,self._active_target))
        return {'status':'ok','draft_id':f'draft_{len(self.drafts)}','confirmed':False}

    def _retained_mapping_conflicts(self):
        """Reuse bounded repair feedback when knowledge changed after a draft."""
        from .object_mapping_feedback import object_mapping_diagnostics
        conflicts=[]
        for index,proposal in enumerate(self.drafts):
            operator,target=self.draft_origins[index]
            diagnostics=object_mapping_diagnostics(proposal,self.packet,
                hierarchy_plan=operator=='submit_hierarchy_plan')
            if not diagnostics:continue
            signature=digest(proposal.model_dump(mode='json'),diagnostics)
            cached=self._mapping_rejections.get(index)
            if not cached or cached[0]!=signature or cached[1]['rejection_id'] not in self.open_rejections:
                previous=self._active_operator,self._active_target
                self._active_operator,self._active_target=operator,target
                try:result=self.reject('object_model_mapping_conflict',diagnostics=diagnostics)
                finally:self._active_operator,self._active_target=previous
                result.update(replace_draft_id=f'draft_{index+1}',draft_operator=operator)
                cached=(signature,result);self._mapping_rejections[index]=cached
            conflicts.append((index,deepcopy(cached[1])))
        return conflicts

    def batch(self, *, pending_code=None, abstain=False):
        # An explicit withdrawal governs the whole current invocation. Earlier
        # drafts are hypotheses, not authority to publish against that decision.
        conflicts=[] if abstain else self._retained_mapping_conflicts()
        blocked={index for index,_ in conflicts}
        exportable=[draft for index,draft in enumerate(self.drafts) if index not in blocked]
        batch = ProposalBatch(task=self.task,proposals=[] if abstain else deepcopy(exportable),
            semantic_schema=self.schema.model_copy(deep=True),
            abstain=abstain or not exportable)
        # Private runtime metadata is not provider-writable and never enters
        # source-derived proposal JSON, rule evidence, or the model transcript.
        codes = set()
        if conflicts:codes.add('object_model_mapping_conflict')
        if pending_code:
            codes.add(pending_code)
        if self.pending:
            codes.add('discovery_submission_pending')
        # Finishing alone cannot hide rejected work. Only a validated correction,
        # explicit ID withdrawal or global abstention closes rejected attempts.
        if self.open_rejections and not abstain:
            codes.add('discovery_invalid_drafts')
        batch._runtime_pending_codes = sorted(codes)
        batch._runtime_invalid_drafts = self.invalid_count
        batch._runtime_invalid_codes = dict(self.invalid_code_counts)
        batch._runtime_object_models = deepcopy(self.object_models)
        batch._runtime_invalid_diagnostics = [dict(code=code,path=list(path),constraint=constraint,count=count)
            for (code,path,constraint),count in self.invalid_diagnostics.items()][:64]
        return batch

    def clear(self):
        self.drafts.clear()
        self.draft_origins.clear()
        self.object_models.clear()
        self._mapping_rejections.clear()
        self.schema = SemanticSchema()
        self.packet = {}
        self.invalid_codes.clear()
        self.invalid_code_counts.clear()
        self.invalid_diagnostics.clear()
        self.open_rejections.clear()
