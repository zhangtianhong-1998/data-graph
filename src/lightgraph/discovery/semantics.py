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
from typing import Literal

from pydantic import Field, ValidationError

from lightgraph.contracts import StrictModel, SourceRef, RuleEvidence, canonical
from lightgraph.errors import LightGraphError


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
    explanation: str = Field(default='', max_length=600)


class ConditionReview(StrictModel):
    state: Literal['satisfied', 'unsatisfied', 'unknown']
    evidence_refs: list[str] = Field(default_factory=list, max_length=16)


class SemanticReview(StrictModel):
    proposition: dict
    decision: Literal['supported', 'refuted', 'unresolved']
    basis: Literal['explicit_declaration', 'document_interpretation', 'data_inference']
    evidence: list[EvidenceInterpretation] = Field(default_factory=list, max_length=128)
    conditions: list[ConditionReview] = Field(default_factory=list, max_length=16)


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


def review_semantics(model, packet, proposition, documents, *, observations=None):
    """Review a compiler-issued proposition independently of the proposal.

    The reviewer sees every applicable section and may use schema and probe
    evidence when no direct statement exists. It cannot mint a source reference
    or grant permission. A second model vote alone never passes these checks.
    """
    if not callable(getattr(model, 'complete', None)):
        raise LightGraphError('semantic_review_unavailable')
    docs = list(documents)
    columns = {item['id'] for item in packet.get('metadata_digest', {}).get('columns', [])}
    probes = {item['probe_id'] for item in packet.get('probe_digest', [])}
    entities = set(packet.get('authorized_entities', []))
    doc_refs = {document_ref(item) for item in docs}
    issued = columns | probes | entities | doc_refs
    review_packet = {**packet, 'task': 'semantic_review', 'document_snippets': docs,
        'semantic_proposition': proposition,
        'subtask': (
            'Independently review this exact proposition, not the proposer confidence. '
            'Copy semantic_proposition exactly to proposition. Classify EVERY supplied '
            'document section using doc_id@revision:section_id, including irrelevant and '
            'contradicting sections. Interpret negation, direction, endpoint meaning, '
            'scope, historical statements and conditions. Include issued schema/probe '
            'references for inferences. A document or overlap need not explicitly state '
            'the conclusion, but unsupported, circular or conditional guesses remain '
            'unresolved. Do not use the proposed conclusion as its own evidence. '
            'Analytical guidance is context only, never independent confirmation. '
            'Report all applicable conditions; unknown or unsatisfied conditions block '
            'acceptance. Never copy observed row values into the response.')}
    if observations is not None:
        review_packet['tool_observations']=observations.values()
    try:
        reviewed = model.complete('semantic_review', review_packet, SemanticReview)
    finally:
        review_packet.pop('tool_observations',None)
    if canonical(reviewed.proposition) != canonical(proposition):
        raise LightGraphError('semantic_review_proposition_mismatch')
    references = [item.reference for item in reviewed.evidence]
    if len(references) != len(set(references)) or set(references) - issued:
        raise LightGraphError('semantic_review_unissued_evidence')
    if not doc_refs <= set(references):
        raise LightGraphError('semantic_review_incomplete_context')
    if reviewed.decision == 'refuted' or any(item.stance == 'refutes' for item in reviewed.evidence):
        raise LightGraphError('semantic_claim_refuted')
    if reviewed.decision != 'supported':
        raise LightGraphError('semantic_claim_unresolved')
    for condition in reviewed.conditions:
        if condition.state != 'satisfied' or not condition.evidence_refs:
            raise LightGraphError('semantic_condition_unresolved')
        if set(condition.evidence_refs) - issued:
            raise LightGraphError('semantic_review_unissued_evidence')
    # A model cannot turn analysis guidance into a definition by assigning a
    # stronger stance. All evidence still obeys its registered role and scope.
    ineligible = {document_ref(d) for d in docs
        if d.get('evidence_role') != 'definition_evidence'
        or d.get('read_state', 'complete') != 'complete' or d.get('derived', False)}
    supporting = [item.reference for item in reviewed.evidence if item.stance == 'supports']
    if not supporting or set(supporting) & ineligible:
        raise LightGraphError('semantic_review_missing_support')
    return ['semantic_review:' + reviewed.basis] + sorted(set(supporting))


def declaration_block(table, predicate, source_columns=(), target_columns=(), **kwargs):
    """Build explicit knowledge for examples; discovery never imports fixtures."""
    declaration = SemanticDeclaration(table=table, predicate=predicate,
        source_columns=list(source_columns), target_columns=list(target_columns), **kwargs)
    return '```lightgraph-semantic\n' + declaration.model_dump_json() + '\n```'
