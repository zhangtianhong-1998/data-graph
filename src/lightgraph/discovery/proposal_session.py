"""Ephemeral, incremental discovery drafts; submission never publishes a rule.

Each local operation validates one typed hypothesis. Source tools still own data
access, and the ordinary compiler retains all semantic and execution gates.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Literal

from pydantic import Field, ValidationError

from lightgraph.contracts import StrictModel
from lightgraph.errors import LightGraphError
from lightgraph.semantic_schema import SemanticSchema
from .models import (ProposalBatch, TASK_PROPOSAL_KINDS, ColumnGroupProposal,
    EntityKeyProposal, HierarchyProposal, LongHierarchyProposal, ReferenceProposal,
    DiscriminatorProposal, ExclusionProposal, ConceptMappingProposal)
from .reference_validation import normalize_column_references, validate_proposal_references
from .validation_feedback import model_diagnostics


class DiscoveryFinish(StrictModel):
    """Small final response; hypotheses have already been submitted as tools."""
    action: Literal['finish_discovery', 'abstain']
    pending: bool = Field(default=False, description=
        'Set true if relevant work remains. False acknowledges rejected attempts '
        'have been corrected or withdrawn; only retained valid drafts proceed to validation.')


PROPOSAL_MODELS = {
    'column_group': ColumnGroupProposal, 'entity_key': EntityKeyProposal,
    'wide_hierarchy': HierarchyProposal, 'long_hierarchy': LongHierarchyProposal,
    'reference': ReferenceProposal, 'discriminator': DiscriminatorProposal,
    'exclusion': ExclusionProposal,
    'concept_mapping': ConceptMappingProposal,
}
MAX_SUBMISSION_CALLS = 16


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
    specs.append(_spec('declare_semantic_schema',
        'Add new vocabulary definitions once. Existing definitions cannot be overwritten.',
        SemanticSchema.model_json_schema()))
    specs.append(_spec('finish_discovery',
        'Finish using all retained drafts. Set pending=true if relevant work remains. '
        'pending=false acknowledges all rejected attempts have been corrected or withdrawn. '
        'Never resubmit the full batch; finishing does not approve semantics.',
        {'type':'object','properties':{'pending':{'type':'boolean','default':False,
            'description':DiscoveryFinish.model_json_schema()['properties']['pending']['description']}},
         'additionalProperties':False}))
    return specs


class ProposalSession:
    """One bounded memory-only ledger, cleared when the Agent invocation exits."""

    def __init__(self, packet):
        self.packet = packet
        self.task = packet['task']
        self.drafts = []
        self.schema = SemanticSchema()
        self.finished = False
        self.pending = False
        self.invalid_count = 0
        self.invalid_codes = set()
        self.names = {item['function']['name'] for item in proposal_tool_specs(self.task)}

    def reject(self, code='invalid_proposal_arguments', *, diagnostics=None):
        self.invalid_count += 1
        self.invalid_codes.add(code)
        result = {'status':'error', 'code':code,
                  'retained_drafts':len(self.drafts), 'confirmed':False}
        if diagnostics:
            result['diagnostics'] = diagnostics
        return result

    def execute(self, name, arguments):
        if self.finished:
            return self.reject('discovery_session_finished')
        contract = None
        payload = arguments
        try:
            if name == 'finish_discovery':
                contract = DiscoveryFinish
                payload = {'action':name, **arguments}
                command = contract.model_validate(payload)
                self.pending = command.pending
                self.finished = True
                return {'status':'ok','finished':True,'retained_drafts':len(self.drafts),
                        'confirmed':False,'pending':self.pending}
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
            if name == 'submit_hierarchy_plan':
                from .hierarchy_plan import HierarchyPlan, compile_hierarchy_plan
                contract = HierarchyPlan
                converted = compile_hierarchy_plan(arguments,self.packet)
                # The adapter supplies only a fallback entity description.
                # Reuse issued or explicitly declared vocabulary verbatim;
                # this exception never applies to user declaration changes.
                known = {item.name:item for item in SemanticSchema.model_validate(
                    self.packet.get('semantic_schema',{})).entity_types}
                known.update({item.name:item for item in self.schema.entity_types})
                converted.semantic_schema.entity_types = [known.get(item.name,item)
                    for item in converted.semantic_schema.entity_types]
                declared = self.execute('declare_semantic_schema',
                    converted.semantic_schema.model_dump(mode='json'))
                if declared['status'] != 'ok':
                    return declared
                return self.execute('submit_wide_hierarchy',
                    converted.proposals[0].model_dump(mode='json',exclude={'kind'}))
            kind = name.removeprefix('submit_')
            if kind not in TASK_PROPOSAL_KINDS[self.task]:
                return self.reject('task_kind_forbidden')
            contract = PROPOSAL_MODELS[kind]
            payload = {'kind':kind, **arguments}
            proposal = contract.model_validate(payload)
            candidate = ProposalBatch(task=self.task,proposals=[proposal])
            candidate, _ = normalize_column_references(candidate,self.packet)
            validate_proposal_references(candidate,self.packet)
            proposal = candidate.proposals[0]
            for index, old in enumerate(self.drafts):
                if old == proposal:
                    return {'status':'ok','draft_id':f'draft_{index+1}',
                            'duplicate':True,'confirmed':False}
            if len(self.drafts) >= 6:
                return self.reject('proposal_draft_limit')
            self.drafts.append(proposal)
            return {'status':'ok','draft_id':f'draft_{len(self.drafts)}','confirmed':False}
        except ValidationError as exc:
            return self.reject('invalid_proposal_arguments', diagnostics=(
                model_diagnostics(exc, contract.model_json_schema(), payload) if contract else None))
        except LightGraphError as exc:
            return self.reject(exc.code)

    def batch(self, *, pending_code=None, abstain=False):
        # An explicit withdrawal governs the whole current invocation. Earlier
        # drafts are hypotheses, not authority to publish against that decision.
        batch = ProposalBatch(task=self.task,proposals=[] if abstain else deepcopy(self.drafts),
            semantic_schema=self.schema.model_copy(deep=True),
            abstain=abstain or not self.drafts)
        # Private runtime metadata is not provider-writable and never enters
        # source-derived proposal JSON, rule evidence, or the model transcript.
        codes = set()
        if pending_code:
            codes.add(pending_code)
        if self.pending:
            codes.add('discovery_submission_pending')
        # Count rejected attempts for telemetry, but an explicit completed
        # session acknowledges they were corrected or withdrawn. No rejected
        # payload is retained or promoted by that acknowledgment.
        if self.invalid_count and not abstain and (not self.finished or self.pending):
            codes.add('discovery_invalid_drafts')
        batch._runtime_pending_codes = sorted(codes)
        batch._runtime_invalid_drafts = self.invalid_count
        return batch

    def clear(self):
        self.drafts.clear()
        self.schema = SemanticSchema()
        self.packet = {}
        self.invalid_codes.clear()
