"""Semantic decisions are separate from structural overlap and proposal wording.

Natural-language evidence is interpreted in a separate bounded model call. The
compiler checks its exact proposition, issued references, document coverage and
unresolved conditions. This is an auditable model judgment, not a proof of truth.
Without a model only explicit, scoped machine-readable declarations can confirm
semantics; keyword matches never do. Requests, excerpts and explanations remain
ephemeral. Only issued evidence references escape this module.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Literal

from pydantic import Field, ValidationError

from lightgraph.contracts import StrictModel, SourceRef, RuleEvidence, canonical
from lightgraph.errors import LightGraphError
from .models import MAX_TRANSIENT_TEXT_CHARS


class SemanticDeclaration(StrictModel):
    version: Literal[1] = 1
    table: str
    predicate: str = Field(min_length=1, max_length=128)
    source_columns: list[str] = Field(default_factory=list, max_length=32)
    target_columns: list[str] = Field(default_factory=list, max_length=32)
    polarity: Literal['affirmed', 'denied'] = 'affirmed'
    conditions: dict = Field(default_factory=dict)
    parameters: dict = Field(default_factory=dict)


class EvidenceInterpretation(StrictModel):
    reference: str
    stance: Literal['supports', 'refutes', 'context', 'irrelevant']
    # Used only in the transient review response; never copied into RuleEvidence.
    explanation: str = Field(default='', max_length=MAX_TRANSIENT_TEXT_CHARS)


class ConditionReview(StrictModel):
    """A business-meaning or applicability condition, not a pending engine check."""
    state: Literal['satisfied', 'unsatisfied', 'unknown']
    evidence_refs: list[str] = Field(default_factory=list, max_length=16)
    # Transient reasoning only, like EvidenceInterpretation.explanation.
    explanation: str = Field(default='', max_length=MAX_TRANSIENT_TEXT_CHARS)
    condition_ref: str | None = Field(default=None, max_length=64,
        description='For a semantic revision, copy the issued prior condition_ref and preserve its condition text.')


class SemanticReview(StrictModel):
    # The compiler binds the full proposition; copying a large hierarchy claim
    # back through the model is unnecessary and prone to omitted defaults.
    proposition_ref: str | None = Field(default=None, max_length=96,
        description='Copy semantic_proposition_ref from the input. Do not restate the proposition.')
    proposition: dict | None = Field(default=None,
        description='Legacy response only. Omit this field when returning proposition_ref.')
    decision_scope: Literal['proposition','document_window'] = 'proposition'
    decision: Literal['supported', 'refuted', 'unresolved']
    basis: Literal['explicit_declaration', 'document_interpretation', 'data_inference','bound_model_review']
    evidence: list[EvidenceInterpretation] = Field(default_factory=list, max_length=128)
    conditions: list[ConditionReview] = Field(default_factory=list, max_length=16)


class BoundEvidence(StrictModel):
    reference: str = Field(description='One issued evidence reference; no copied source text.')
    stance: Literal['supports', 'refutes', 'context', 'irrelevant']


class BoundCondition(StrictModel):
    state: Literal['satisfied', 'unsatisfied', 'unknown']
    evidence_refs: list[str] = Field(default_factory=list,max_length=16)
    condition: str = Field(min_length=1,max_length=MAX_TRANSIENT_TEXT_CHARS,
        description='One concise business condition per entry. No reasoning narrative, '
        'source values or multiple conditions in one entry.')
    condition_ref: str | None = Field(default=None, max_length=64,
        description='Copy the issued prior condition_ref when revising; do not omit or rewrite its condition.')


class BoundSemanticVerdict(StrictModel):
    """The caller owns the proposition; the model supplies only its verdict."""
    decision_scope: Literal['proposition','document_window'] = 'proposition'
    decision: Literal['supported','refuted','unresolved']
    evidence: list[BoundEvidence] = Field(default_factory=list,max_length=128,
        description='Classify every supplied document section, including counterevidence.')
    conditions: list[BoundCondition] = Field(default_factory=list,max_length=16,
        description='All actual applicability conditions; keep unknown or unsatisfied states.')

    def bind(self, proposition_ref):
        return SemanticReview(proposition_ref=proposition_ref,decision_scope=self.decision_scope,
            decision=self.decision,basis='bound_model_review',
            evidence=[item.model_dump() for item in self.evidence],
            conditions=[{'state':item.state,'evidence_refs':item.evidence_refs,
                         'explanation':item.condition,'condition_ref':item.condition_ref} for item in self.conditions])


class EvidenceReferenceReplacement(StrictModel):
    invalid_reference: str
    issued_reference: str


class SemanticReferencePatch(StrictModel):
    """Repair locators without asking for a second semantic judgment."""
    replacements: list[EvidenceReferenceReplacement] = Field(default_factory=list,max_length=384)
    document_classifications: list[BoundEvidence] = Field(default_factory=list,max_length=128)


def document_ref(document):
    return document['doc_id'] + '@' + document['revision'] + ':' + document['section_id']


def applicable_definition(document, table):
    source = table if isinstance(table, SourceRef) else SourceRef(
        source_id=table.source_id, schema=table.schema_name, table=table.name)
    applicable = ('sources' not in document or any(
        item.get('source_id') == source.source_id
        and item.get('schema', item.get('schema_name', 'public')) == source.schema_name
        and item.get('table') == source.table for item in document['sources']))
    return (applicable and document.get('evidence_role', 'definition_evidence') == 'definition_evidence'
        and document.get('read_state', 'complete') == 'complete' and not document.get('derived', False))


def declarations(documents, table):
    """Read only a named, closed declaration format, never free prose or YAML tags."""
    for document in documents:
        if not applicable_definition(document, table):
            continue
        for block in re.findall(r'^```lightgraph-semantic\s*\n(.*?)^```\s*$', document['text'], re.M | re.S):
            try:
                raw = json.loads(block)
                entries = raw if isinstance(raw, list) else [raw]
                if len(entries) > 32:
                    continue
                checked = [SemanticDeclaration.model_validate(item) for item in entries]
            except (ValueError, TypeError, ValidationError):
                continue
            for declaration in checked:
                if declaration.table == table.qualified_name:
                    columns = set(declaration.source_columns + declaration.target_columns)
                    if columns <= set(table.column_names):
                        yield declaration, document_ref(document)


def declared_evidence(documents, table, predicate, source_columns=(), target_columns=(), *,
                      conditions=None, parameters=None):
    """Match an exact proposition; any applicable denial blocks an affirmation."""
    matches = [(item, ref) for item, ref in declarations(documents, table)
        if item.predicate == predicate and item.source_columns == list(source_columns)
        and item.target_columns == list(target_columns)
        and item.conditions == (conditions or {}) and item.parameters == (parameters or {})]
    if any(item.polarity == 'denied' for item, _ in matches):
        return []
    return sorted({ref for item, ref in matches if item.polarity == 'affirmed'})


def rule_evidence(refs=(), *, reviewed=False, documents=(), probe_refs=()):
    """Construct the persisted, value-free decision summary at the compiler edge."""
    refs=list(refs)
    return RuleEvidence(origin='discovery',semantic_basis=refs,probe_refs=list(probe_refs),
        semantic_decision=('model_review' if reviewed else 'explicit_declaration') if refs else 'unreviewed',
        semantic_status='supported' if refs else 'unresolved',
        reviewed_context_refs=sorted({document_ref(d) for d in documents}) if reviewed else list(refs),
        conditions_satisfied=bool(refs))


def hierarchy_identity_context(batch, table, rules=()):
    """Describe known hierarchy positions using issued columns, never row keys.

    Read the whole proposal batch before compiling any sibling so entity-grain
    review does not depend on proposal order. Accepted rules carry the same
    context into later contract and FD repair tasks.
    """
    from .engine import _column_id, _source
    contexts=[]
    for proposal in batch.proposals:
        claim=proposal.claim
        if proposal.kind not in ('wide_hierarchy','long_hierarchy'):
            continue
        keys=([level.key for level in claim.levels] if proposal.kind=='wide_hierarchy'
              else [claim.key_column,claim.parent_column])
        contexts.append({'state':'proposed','kind':proposal.kind,
            'entity_kind':claim.entity_kind_hint,'position_keys':list(dict.fromkeys(keys)),
            'namespace_columns':claim.namespace_columns,
            'identity_domain':getattr(claim,'identity_domain',''),
            'identity_scope':getattr(claim,'identity_scope','namespace_local'),
            'identity_resolution':(claim.identity_resolution if proposal.kind=='wide_hierarchy'
                and claim.identity_resolution is not None else 'verified_identity_domain'
                if getattr(claim,'path_semantics','legacy')=='relative_path' else 'source_scoped'),
            'claim':claim.model_dump(mode='json')})
    for rule in rules:
        if not rule.hierarchy or rule.source!=_source(table):
            continue
        columns=[level.code for level in rule.hierarchy.levels]
        if rule.hierarchy.parent_column:
            columns+=rule.identity.key_columns+[rule.hierarchy.parent_column]
        columns=[c for c in dict.fromkeys(columns) if c not in rule.scope.namespaces]
        contexts.append({'state':'compiled','kind':rule.kind,'rule_id':rule.rule_id,
            'entity_kind':rule.identity.entity_kind,
            'position_keys':[_column_id(table,c) for c in columns],
            'namespace_columns':[_column_id(table,c) for c in rule.scope.namespaces],
            'identity_domain':rule.identity.identity_domain,
            'identity_scope':rule.identity.identity_scope,
            'identity_resolution':rule.identity.semantic_resolution,
            'level_policy':rule.identity.level_policy,'taxonomy':rule.hierarchy.taxonomy,
            'path_semantics':'relative_path' if rule.identity.level_policy=='relative_path' else 'legacy',
            'position_semantics':rule.hierarchy.position_semantics,
            'context_qualifiers':rule.hierarchy.context_qualifiers,
            'allow_dag':rule.hierarchy.allow_dag,
            'repeat_policy':'collapse_consecutive_equal_identity' if rule.hierarchy.fill_semantics_confirmed else 'none'})
    return contexts


def entity_granularity_identity(proposal):
    """Identify an entity grain independently of currently visible hypotheses."""
    claim=proposal.claim
    if proposal.kind=='column_group':
        keys={m.column_id for m in claim.members if m.role=='namespace' or m.in_identity and (
            m.role=='identity_code' or m.role=='semantic_attribute' and m.property_name=='business_code')}
    elif proposal.kind=='entity_key':
        keys=set(claim.key_columns)
    else:
        return None
    return claim.entity_kind_hint,tuple(sorted(keys))


def entity_granularity_context(proposal, hierarchies):
    """Find possible path-as-entity identities, without rejecting composite keys."""
    identity=entity_granularity_identity(proposal)
    if identity is None:
        return None
    kind,columns=identity
    keys=set(columns)
    conflicts=[context for context in hierarchies
        if context['entity_kind']==kind and
        len(keys & (set(context['position_keys'])-set(context['namespace_columns'])))>=2]
    if not conflicts:
        return None
    return {'identity_columns':sorted(keys),'hierarchies':conflicts}


def entity_granularity_context_signatures(context):
    """Compare semantic hierarchy context without cosmetic IDs or state changes."""
    if not context:
        return set()
    signatures=set()
    for hierarchy in context['hierarchies']:
        claim=hierarchy.get('claim',{})
        semantic={key:hierarchy.get(key,claim.get(key,default)) for key,default in (
            ('kind',''),('entity_kind',''),('identity_domain',''),('identity_scope','namespace_local'),
            ('taxonomy',''),('path_semantics','legacy'),('position_semantics',None),
            ('context_qualifiers',{}),('allow_dag',False),('repeat_policy','none'))}
        resolution=hierarchy.get('identity_resolution',claim.get('identity_resolution'))
        semantic['identity_resolution']=(resolution if resolution is not None else
            'verified_identity_domain' if semantic['path_semantics']=='relative_path' else 'source_scoped')
        semantic['position_keys']=hierarchy['position_keys']
        semantic['namespace_columns']=sorted(hierarchy['namespace_columns'])
        signatures.add(canonical(semantic))
    return signatures


def review_entity_granularity(model, packet, proposal, context, documents, *, observations=None):
    """Review business-object grain separately from FD/tuple uniqueness."""
    proposition={'kind':'entity_granularity','scope':packet.get('scope'),
        'candidate':proposal.model_dump(mode='json'),'identity_columns':context['identity_columns'],
        'hierarchy_identity_context':context['hierarchies']}
    refs=review_semantics(model,packet,proposition,documents,observations=observations)
    return ['semantic_review:entity_granularity',*refs]


_REPAIRABLE_REVIEW_CODES = frozenset({
    'semantic_review_unissued_evidence', 'semantic_review_incomplete_context',
})


def _check_review(reviewed, *, proposition_ref, expected_proposition, docs, doc_refs, issued,
                  allow_no_support=False, complete_collection=False):
    """Validate one judgment without interpreting a model echo as the claim."""
    # A negative or undecided judgment is not a formatting problem to retry
    # until it turns positive, even when other fields are malformed.
    if reviewed.decision == 'refuted' or any(item.stance == 'refutes' for item in reviewed.evidence):
        raise LightGraphError('semantic_claim_refuted')
    neutral_window=(allow_no_support and reviewed.decision_scope=='document_window'
        and all(item.stance in ('context','irrelevant') for item in reviewed.evidence))
    if reviewed.decision != 'supported' and not neutral_window:
        raise LightGraphError('semantic_claim_unresolved')
    if any(item.state!='satisfied' or not item.evidence_refs for item in reviewed.conditions):
        raise LightGraphError('semantic_condition_unresolved')
    for condition in reviewed.conditions:
        if set(condition.evidence_refs) - issued:
            raise LightGraphError('semantic_review_unissued_evidence')
    if reviewed.proposition_ref is not None:
        if reviewed.proposition_ref != proposition_ref:
            raise LightGraphError('semantic_review_reference_mismatch')
    elif reviewed.proposition is None:
        raise LightGraphError('semantic_review_proposition_missing')
    # Older callers can still echo the complete claim. If a response supplies
    # both forms, a conflicting echo must not be hidden by a correct reference.
    if reviewed.proposition is not None and canonical(reviewed.proposition) != expected_proposition:
        raise LightGraphError('semantic_review_proposition_mismatch')
    references = [item.reference for item in reviewed.evidence]
    if len(references) != len(set(references)) or set(references) - issued:
        raise LightGraphError('semantic_review_unissued_evidence')
    if not doc_refs <= set(references):
        raise LightGraphError('semantic_review_incomplete_context')
    ineligible = {document_ref(d) for d in docs
        if d.get('evidence_role') != 'definition_evidence'
        or d.get('read_state', 'complete') != 'complete' and not complete_collection or d.get('derived', False)}
    supporting = [item.reference for item in reviewed.evidence if item.stance == 'supports']
    if (not supporting and not allow_no_support) or set(supporting) & ineligible:
        raise LightGraphError('semantic_review_missing_support')
    return ['semantic_review:' + reviewed.basis] + sorted(set(supporting)) if supporting else []


def _review_stage_contract(proposition):
    """Describe later implemented checks without claiming they have passed."""
    candidate=proposition.get('candidate',{}) if proposition.get('kind')=='entity_granularity' else proposition
    kind=candidate.get('kind')
    claim=candidate.get('claim',{})
    checks=[]
    if kind in {'column_group','entity_key','wide_hierarchy','long_hierarchy','reference'}:
        checks=['source_snapshot_consistency','complete_projected_source_read']
    if kind in {'column_group','entity_key','long_hierarchy'}:
        checks+=['identity_key_nulls','single_valued_attribute_fd']
    if kind=='wide_hierarchy':
        checks+=['hierarchy_code_label_consistency','hierarchy_repeat_and_gap_policy']
        if claim.get('path_semantics')=='relative_path' and claim.get('namespace_columns'):
            checks.append('identity_namespace_nulls')
    if kind=='long_hierarchy':
        checks.append('hierarchy_parent_reference_resolution')
    if kind in {'wide_hierarchy','long_hierarchy'}:
        checks+=['hierarchy_self_loop_cycle_and_parent_policy','prospective_graph_topology']
    if kind=='reference':
        checks+=['reference_endpoint_resolution','reference_null_and_array_policy',
                 'reference_self_link_policy','prospective_endpoint_visibility']
    return {'stage':'semantic_interpretation_before_rule_execution',
        'supported_decision_allows':'draft_rule_compilation_only',
        'publication_authorized':False,
        'programmatic_checks':{'state':'pending_if_rule_compiles','checks':checks}}


def _review_reference_state(reviewed, issued, doc_refs):
    references={item.reference for item in reviewed.evidence}
    condition_refs={ref for item in reviewed.conditions for ref in item.evidence_refs}
    return (references | condition_refs) - issued, doc_refs - references


def _apply_reference_patch(reviewed, patch, *, issued, doc_refs):
    """Only invalid locators and absent document classifications are writable."""
    invalid,missing_docs=_review_reference_state(reviewed,issued,doc_refs)
    replacements={item.invalid_reference:item.issued_reference for item in patch.replacements}
    added=[item.reference for item in patch.document_classifications]
    if (len(replacements)!=len(patch.replacements) or set(replacements)-invalid
            or set(replacements.values())-issued or len(added)!=len(set(added))
            or set(added)-missing_docs):
        raise LightGraphError('semantic_review_patch_invalid')
    repaired=reviewed.model_copy(deep=True)
    for item in repaired.evidence:
        item.reference=replacements.get(item.reference,item.reference)
    for item in repaired.conditions:
        item.evidence_refs=[replacements.get(ref,ref) for ref in item.evidence_refs]
    repaired.evidence.extend(EvidenceInterpretation(**item.model_dump())
        for item in patch.document_classifications)
    # The same invalid locator can be an alias of an existing classification.
    # Collapse identical entries only; conflicting stances still fail validation.
    repaired.evidence=list({canonical(item.model_dump()):item for item in repaired.evidence}.values())
    try:
        return SemanticReview.model_validate(repaired.model_dump())
    except ValidationError:
        raise LightGraphError('semantic_review_patch_invalid') from None


def _review_semantics_window(model, packet, proposition, documents, *, observations=None,
                             batch=None):
    """Review a compiler-issued proposition independently of the proposal.

    The reviewer sees every applicable section and may use schema and probe
    evidence when no direct statement exists. It cannot mint a source reference
    or grant permission. A second model vote alone never passes these checks.
    """
    bound_complete=getattr(model,'complete_bound_review',None)
    bound=callable(bound_complete)
    if not bound and not callable(getattr(model, 'complete', None)):
        raise LightGraphError('semantic_review_unavailable')
    docs = list(documents)
    columns = {item['id'] for item in packet.get('metadata_digest', {}).get('columns', [])}
    probes = {item['probe_id'] for item in packet.get('probe_digest', [])}
    entities = set(packet.get('authorized_entities', []))
    doc_refs = {document_ref(item) for item in docs}
    issued = columns | probes | entities | doc_refs
    expected_proposition = canonical(proposition)
    proposition_ref = 'proposition_' + uuid.uuid4().hex
    format_instruction=('The caller binds this full proposition. Return only the verdict, '
        'evidence reference/stance pairs and one short statement per business condition. '
        'Do not return a proposition, proposition_ref, basis or reasoning narrative. '
        if bound else 'Return its semantic_proposition_ref as proposition_ref; '
        'omit proposition rather than copying or rewriting the full claim. ')
    review_packet = {**packet, 'task': 'semantic_review', 'document_snippets': docs,
        'document_refs':[],
        'issued_evidence_refs':sorted(issued),
        'required_document_refs':sorted(doc_refs),
        'semantic_proposition': json.loads(expected_proposition),
        'semantic_proposition_ref': proposition_ref,
        'review_stage_contract':_review_stage_contract(proposition),
        'subtask': (
            'Independently review this exact proposition, not the proposer confidence. '
            'The compiler owns semantic_proposition, including its endpoints, order, '
            'scope and conditions. '+format_instruction+
            'Classify EVERY supplied '
            'document section using the exact required_document_refs (also listed in issued_evidence_refs), including irrelevant and '
            'contradicting sections. Interpret negation, direction, endpoint meaning, '
            'scope, historical statements and conditions. Include issued schema/probe '
            'references for inferences. A document or overlap need not explicitly state '
            'the conclusion, but unsupported, circular or conditional guesses remain '
            'unresolved. Do not use the proposed conclusion as its own evidence. '
            'Analytical guidance is context only, never independent confirmation. '
            'Review business interpretation and applicability at this stage. A supported decision '
            'allows draft rule compilation only, never graph publication. The review_stage_contract '
            'lists implemented programmatic checks that remain pending if the rule compiles; '
            'their results are not asserted here. Do not turn the mere absence of a future full '
            'FD, null-key or graph-topology check into an unknown business condition. Structural '
            'checks cannot establish semantic meaning, and a semantic review cannot waive them. '
            'Report every actual business-meaning, scope or applicability condition in conditions; '
            'unknown or unsatisfied business conditions still block acceptance. Existing observations '
            'that contradict the interpretation remain counterevidence; never defer or ignore them '
            'because more validation is scheduled. Do not delete a genuine condition or mark it '
            'satisfied without evidence. Never copy observed row values into the response.')}
    if proposition.get('kind')=='entity_granularity':
        review_packet['subtask'] += (
            ' This candidate combines multiple positions of a same-kind hierarchy into one identity. '
            'Independently decide whether that composite identifies the business object requested by '
            'the goal, or merely identifies an entire row/path/occurrence. Check the complete candidate, '
            'goal exclusions, namespaces, known hierarchy identity domains and observed meaning. '
            'FD success, DISTINCT uniqueness or adding more position keys to eliminate conflicts '
            'does not establish entity granularity. Legitimate business composite keys may be supported '
            'when evidence establishes their intended grain; do not reject them merely for being composite. '
            'Proposed hierarchy context is a competing hypothesis, not self-confirming evidence. '
            'Refute a grain excluded by the goal; keep uncertain grain unresolved.')
    if packet.get('hierarchy_identity_requests'):
        review_packet['subtask'] += (
            ' hierarchy_identity_requests retains earlier conflicting identity requests as '
            'unconfirmed hypotheses. Review whether this repaired full claim resolves their '
            'meaning using independent evidence; they are not supporting evidence. Do not '
            'ignore an original restriction or condition merely because the repair omits it. '
            'source_scoped and namespace_local are not equivalent: namespace-local verified '
            'identity domains can share identities across source tables.')
    if packet.get('iteration_context',{}).get('rule_conflict_requests'):
        review_packet['subtask'] += (
            ' This is an adjudication of conflicting field mappings. Compare BOTH retained '
            'alternatives in iteration_context.rule_conflict_requests with the revised '
            'proposition and the supplied definitions and probes. The alternatives are '
            'hypotheses, not evidence. Establish why the chosen column names this object '
            'and which conflicting interpretation the evidence rules out. Preserve all '
            'scope, exclusion and applicability obligations; omission is not resolution. '
            'FD consistency or a distinct count alone cannot establish a name mapping. '
            'If the evidence cannot distinguish the alternatives, return unresolved.')
    if packet.get('semantic_revision_guard'):
        review_packet['subtask'] += (
            ' This is a structurally changed alternative to a rejected claim. The '
            'semantic_revision_guard contains the original counterevidence and conditions. '
            'Explicitly classify that counterevidence against the new structure. Preserve '
            'each original condition_ref and its exact condition text; every condition '
            'must have independently justified satisfied evidence. Renaming a condition '
            'or omitting it cannot resolve the rejection.')
    if proposition.get('kind')=='reference':
        review_packet['subtask'] += (
            ' The compiler-bound relation_definition is part of this exact proposition, '
            'including its description, endpoint kinds and allow_self_links policy. '
            'Business reference relations forbid an object relating to itself by default. '
            'When allow_self_links is true, independently establish whether reflexive '
            'relationships are valid under this precise business meaning and scope. '
            'Do not authorize them solely because endpoint values match, a source join '
            'succeeds, or the proposer requests permission. Refute unsupported meaning '
            'and keep uncertain permission unresolved; report every actual condition. '
            'Do not rewrite this definition or waive the subsequent identity-level '
            'self-link validation. A supported review of a false policy does not allow '
            'self-links.')
    if batch is not None:
        review_packet['document_batch']=batch
        review_packet['subtask'] += (' This is one bounded window of a complete document collection. '
            'Partial sections have explicit character locators; do not assume omitted text is absent. '
            'Classify only the supplied portions. Set decision_scope=document_window when the decision '
            'only describes this window; a context-only or irrelevant window may be unresolved '
            'because it supplies no support. Use decision_scope=proposition for genuine unresolved '
            'semantics, and always report unknown conditions. A supported decision with no supporting references '
            'means this window adds no support or contradiction; the compiler still requires support '
            'and successful review of every other window. Any uncertain condition or possible '
            'contradiction remains unresolved. Never infer support merely from batching.')
    try:
        if observations is not None:
            review_packet['tool_observations']=observations.values()
        corrections_before=getattr(model,'usage',{}).get('schema_repair_responses',0)
        reviewed=(bound_complete(review_packet,proposition_ref=proposition_ref) if bound else
            model.complete('semantic_review',review_packet,SemanticReview))
        check_args=dict(proposition_ref=proposition_ref,expected_proposition=expected_proposition,
            docs=docs,doc_refs=doc_refs,issued=issued,allow_no_support=batch is not None,
            complete_collection=batch is not None)
        def checked(value):
            refs = _check_review(value, **check_args)
            if packet.get('semantic_revision_guard'):
                from .semantic_revision import validate_prior_obligations
                validate_prior_obligations(value, packet['semantic_revision_guard'], issued)
            return refs
        try:
            return checked(reviewed)
        except LightGraphError as exc:
            corrections_after=getattr(model,'usage',{}).get('schema_repair_responses',0)
            if (corrections_after>corrections_before or reviewed.decision!='supported'
                    or exc.code not in _REPAIRABLE_REVIEW_CODES):
                raise
            invalid,missing_docs=_review_reference_state(reviewed,issued,doc_refs)
            if not invalid and not missing_docs:
                # Identical duplicate references need no model judgment. Other
                # conflicts remain failures; a patch cannot change their stance.
                repaired=_apply_reference_patch(reviewed,SemanticReferencePatch(),
                    issued=issued,doc_refs=doc_refs)
                return checked(repaired)
            if not callable(getattr(model,'complete',None)):
                raise
            patch_packet={**review_packet,'semantic_review_repair':{
                'attempt':1,'error_code':exc.code,
                'invalid_evidence_refs':sorted(invalid),
                'allowed_evidence_refs':sorted(issued),
                'required_document_refs':sorted(missing_docs),
                'fixed_evidence':[{'reference':item.reference,'stance':item.stance}
                    for item in reviewed.evidence],
                'condition_reference_uses':[{'evidence_refs':item.evidence_refs,
                    'condition':item.explanation} for item in reviewed.conditions
                    if set(item.evidence_refs)&invalid],
            },'subtask':(
                'Return only a SemanticReferencePatch for the existing review. Do not return '
                'a new verdict, proposition, conditions or a reasoning narrative. The compiler '
                'preserves the original decision, scope, conditions and evidence interpretations. '
                'Map only invalid_evidence_refs to allowed_evidence_refs when they refer to the '
                'same evidence. Existing issued references and their stances cannot be edited. '
                'Classify each required_document_ref using supports, refutes, context or irrelevant; '
                'interpret negation, applicability, scope and counterevidence against the exact '
                'semantic_proposition. Counterevidence must be refutes. Do not classify a document '
                'that is already present, nor add any other evidence. If a locator cannot be '
                'resolved or a missing document cannot be classified confidently, leave it absent '
                'so validation remains unresolved. Do not invent support to complete the patch. '
                'All text is transient; never copy observed source values into the response.')}
            try:
                patch_complete=getattr(model,'complete_reference_patch',None)
                usage=getattr(model,'usage',None)
                if isinstance(usage,dict):
                    usage['semantic_reference_repair_attempts']=usage.get('semantic_reference_repair_attempts',0)+1
                try:
                    patch=(patch_complete(patch_packet,SemanticReferencePatch) if callable(patch_complete)
                        else model.complete('semantic_review',patch_packet,SemanticReferencePatch))
                except ValidationError:
                    raise LightGraphError('semantic_review_patch_invalid') from None
                if not isinstance(patch,SemanticReferencePatch):
                    raise LightGraphError('semantic_review_patch_invalid')
                repaired=_apply_reference_patch(reviewed,patch,issued=issued,doc_refs=doc_refs)
                result=checked(repaired)
                if isinstance(usage,dict):
                    usage['semantic_reference_repairs_succeeded']=usage.get('semantic_reference_repairs_succeeded',0)+1
                return result
            except LightGraphError:
                if isinstance(usage,dict):
                    usage['semantic_reference_repairs_failed']=usage.get('semantic_reference_repairs_failed',0)+1
                raise
            finally:
                patch_packet.pop('tool_observations',None)
                patch_packet.pop('semantic_review_repair',None)
    except LightGraphError as exc:
        from .semantic_revision import SEMANTIC_FAILURES, SemanticReviewFailure
        if exc.code in SEMANTIC_FAILURES:
            kind = proposition.get('kind')
            stage = ('hierarchy' if kind in ('wide_hierarchy','long_hierarchy') else
                     kind if kind in ('entity_granularity','reference','concept_mapping') else 'entity_definition')
            raise SemanticReviewFailure(exc.code, packet=packet, documents=docs,
                reviewed=locals().get('repaired', locals().get('reviewed')), stage=stage) from None
        raise
    finally:
        review_packet.pop('tool_observations',None)


def review_semantics(model, packet, proposition, documents, *, observations=None):
    """Every document window must pass before any positive decision escapes.

    Text stays in memory and each provider call keeps the original budget.
    A contradiction or unknown condition in a later chunk blocks all earlier
    support. Exhaustion never publishes a result from the processed prefix.
    """
    from lightgraph.context import document_batches
    documents=list(documents)
    if any(d.get('read_state','complete')!='complete' for d in documents):
        raise LightGraphError('semantic_review_incomplete_context')
    if len({document_ref(d) for d in documents})>128:
        raise LightGraphError('semantic_review_document_limit')
    windows=list(document_batches(documents)) or [[]]
    if len(windows)==1 and not any(d.get('read_state')=='partial' for d in windows[0]):
        return _review_semantics_window(model,packet,proposition,windows[0],observations=observations)
    evidence=[]
    for index, window in enumerate(windows):
        evidence.extend(_review_semantics_window(model,packet,proposition,window,
            observations=observations,batch={'index':index+1,'total':len(windows),
                'acceptance_requires_all_windows':True}))
    if not any(not ref.startswith('semantic_review:') for ref in evidence):
        raise LightGraphError('semantic_review_missing_support')
    return sorted(set(evidence))


def declaration_block(table, predicate, source_columns=(), target_columns=(), **kwargs):
    """Build explicit knowledge for examples; discovery never imports fixtures."""
    declaration = SemanticDeclaration(table=table, predicate=predicate,
        source_columns=list(source_columns), target_columns=list(target_columns), **kwargs)
    return '```lightgraph-semantic\n' + declaration.model_dump_json() + '\n```'
