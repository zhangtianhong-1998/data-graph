"""One fresh semantic alternative, with the original rejection kept in scope.

Only issued references enter PendingCandidate. The rejected hypothesis and
condition text exist solely in the current invocation's memory and prompts.
"""
from copy import deepcopy

from lightgraph.contracts import SourceRef, canonical, digest
from lightgraph.errors import LightGraphError
from .models import PendingCandidate, ProposalBatch


SEMANTIC_FAILURES = frozenset({
    'semantic_claim_refuted', 'semantic_claim_unresolved', 'semantic_condition_unresolved',
    'semantic_review_missing_support', 'semantic_review_incomplete_context',
})


def issued_references(packet, documents):
    columns = {c['id'] for c in packet.get('metadata_digest', {}).get('columns', [])}
    rules = set(packet.get('authorized_entities', []))
    docs = {f"{d['doc_id']}@{d['revision']}:{d['section_id']}" for d in documents}
    probes = {p['probe_id'] for p in packet.get('probe_digest', [])}
    return columns, rules, docs, columns | rules | docs | probes


class SemanticReviewFailure(LightGraphError):
    """Transient review feedback; Exception.args/string contain only the code."""
    def __init__(self, code, *, packet, documents, reviewed=None, stage='entity_definition'):
        super().__init__(code)
        _, _, docs, issued = issued_references(packet, documents)
        evidence = list(getattr(reviewed, 'evidence', ()))
        self.stage = stage
        self.blocking = sorted({e.reference for e in evidence if e.stance == 'refutes'} & issued)
        classified = {e.reference for e in evidence}
        self.required = sorted((docs - classified) | {ref for c in getattr(reviewed, 'conditions', ())
            if c.state != 'satisfied' for ref in c.evidence_refs if ref in issued})
        self.conditions = [{'condition_ref': f'condition_{index+1}', 'condition': c.explanation,
            'prior_state': c.state, 'evidence_refs': sorted(set(c.evidence_refs) & issued)}
            for index, c in enumerate(getattr(reviewed, 'conditions', ()))]

    def feedback(self):
        return {'failure_code': self.code, 'blocking_evidence_refs': self.blocking,
                'required_evidence_refs': self.required, 'conditions': deepcopy(self.conditions)}


def _claim(proposal):
    claim = proposal.claim.model_dump(mode='json')
    claim.pop('group_id', None)
    return claim


def candidate_record(proposal, table, packet, documents, failure):
    from .conflicts import proposal_mapping
    columns, rules, docs, issued = issued_references(packet, documents)
    # Intersect every string leaf with issued references; arbitrary model text
    # cannot become a persisted reference, even when it looks like an ID.
    leaves = set()
    def collect(value):
        if isinstance(value, str): leaves.add(value)
        elif isinstance(value, dict):
            for child in value.values(): collect(child)
        elif isinstance(value, list):
            for child in value: collect(child)
    collect(_claim(proposal))
    # IDs exclude presentation/group names and model commentary. The digest is
    # opaque; the stored record contains only permitted schema references.
    shape = revision_shape(proposal)
    source = SourceRef(source_id=table.source_id, schema=table.schema_name, table=table.name)
    return PendingCandidate(candidate_id='candidate_' + digest(source.model_dump(), proposal.kind, shape)[:24],
        source=source, proposal_kind=proposal.kind, failure_code=failure.code,
        column_refs=sorted(leaves & columns), document_refs=sorted(docs)[:128],
        rule_refs=sorted(leaves & rules), blocking_evidence_refs=sorted(set(failure.blocking) & issued)[:128],
        required_evidence_refs=sorted(set(failure.required) & issued)[:128], review_stage=failure.stage,
        mappings=[proposal_mapping(proposal,packet)],
        dependency_fingerprint=packet.get('iteration_context',{}).get('candidate_dependency'))


def revision_shape(proposal):
    """Executable semantic fields only; cosmetic changes cannot create a vote."""
    claim = _claim(proposal)
    for name in ('ordering_evidence', 'identity_evidence', 'attribute_evidence', 'taxonomy_evidence',
                 'position_evidence', 'context_evidence', 'dag_evidence', 'global_scope_evidence'):
        claim.pop(name, None)
    if proposal.kind == 'column_group':
        claim['members'] = sorted(claim['members'], key=lambda m: canonical(m))
    return claim


def eligible_alternative(original, alternative):
    """Admit only explicit structural changes while freezing scope/conditions.

    We do not decide that an alternative is true here. A fresh review must still
    satisfy the original condition obligations and inspect the counterevidence.
    """
    if original.kind != alternative.kind:
        return False
    before, after = revision_shape(original), revision_shape(alternative)
    if before == after:
        return False
    kind = original.kind
    if kind == 'wide_hierarchy':
        # Reordering the same issued positions changes parent interpretation;
        # identity policy, domains, context and every level's columns stay fixed.
        left, right = before.pop('levels'), after.pop('levels')
        def level_fields(levels):
            return sorted([{k: v for k, v in x.items() if k != 'position'} for x in levels], key=canonical)
        return before == after and level_fields(left) == level_fields(right) and left != right
    if kind == 'long_hierarchy':
        left = (before.pop('key_column'), before.pop('parent_column'))
        right = (after.pop('key_column'), after.pop('parent_column'))
        return before == after and left == tuple(reversed(right)) and left != right
    if kind == 'reference':
        fields = ('source_columns', 'target_columns', 'source_level', 'target_level')
        left = {k: before.pop(k) for k in fields}
        right = {k: after.pop(k) for k in fields}
        return before == after and left != right
    if kind == 'entity_key':
        left, right = before.pop('key_columns'), after.pop('key_columns')
        return before == after and set(right) < set(left)
    if kind == 'column_group':
        left, right = before.pop('members'), after.pop('members')
        if before != after: return False
        old_keys = {m['column_id'] for m in left if m['in_identity']}
        new_keys = {m['column_id'] for m in right if m['in_identity']}
        # Splitting a path identity may remove identity columns, not reclassify
        # excluded/fact columns or add a key to hide a counterexample.
        return new_keys < old_keys and all(m in left for m in right)
    return False


def validate_prior_obligations(reviewed, guard, issued):
    """A renamed condition or missing counterevidence cannot pass a revision."""
    evidence = {item.reference for item in reviewed.evidence}
    if not set(guard.get('blocking_evidence_refs', ())) <= evidence:
        raise LightGraphError('semantic_revision_constraints_changed')
    for prior in guard.get('conditions', ()):
        matches = [c for c in reviewed.conditions if c.condition_ref == prior['condition_ref']]
        if (len(matches) != 1 or matches[0].explanation != prior['condition'] or
                matches[0].state != 'satisfied' or not matches[0].evidence_refs or
                set(matches[0].evidence_refs) - issued):
            raise LightGraphError('semantic_revision_constraints_changed')


def revise_once(records, *, model, packet, tool_factory, ask, observations, result, table, documents):
    """Offer one bounded repair session; return matched structural alternatives."""
    if not records or tool_factory is None or not callable(getattr(model, 'explore', None)):
        return [], documents
    # Caller-owned maximum is one new Agent invocation for this batch, with the
    # normal shared model/time/tool allowance. There is no recursive repair.
    requests = [{'candidate_id': item['candidate'].candidate_id,
        'proposal': item['proposal'].model_dump(mode='json', exclude={'rationale', 'requested_probes'}),
        'feedback': item['failure'].feedback()} for item in records]
    context = {**packet.get('iteration_context', {}), 'semantic_revision_requests': requests,
               'semantic_revision_attempt': 1}
    repair_packet = {**packet, 'task': 'T5_revision', 'iteration_context': context,
        'subtask': '一次修订被语义审查拒绝的候选。仅提交有依据的实际结构变更；原判断仍约束原主张。'
        '不得换名字、改类型、删条件或重述原主张寻求新投票。保留所有反证和原条件；无法修订则弃权。'}
    tools = tool_factory()
    try:
        result.stats['semantic_revision_attempts'] = result.stats.get('semantic_revision_attempts', 0) + 1
        batch = ask(model, repair_packet, tools, observations)
        revised_docs = tools.context_documents()
        by_ref = {f"{d['doc_id']}@{d['revision']}:{d['section_id']}": d for d in documents + revised_docs}
        documents = list(by_ref.values())
        if batch.abstain: return [], documents
        alternatives = []
        selected = set()
        for proposal in batch.proposals:
            if proposal.kind == 'exclusion': continue
            matches = [item for item in records if eligible_alternative(item['proposal'], proposal)]
            if len(matches) != 1:
                result.issues.append({'table': table.qualified_name, 'code': 'semantic_revision_no_progress', 'status': 'unresolved'})
                continue
            item = matches[0]
            if item['candidate'].candidate_id in selected:
                continue
            selected.add(item['candidate'].candidate_id)
            alternatives.append((item, proposal, batch.semantic_schema, repair_packet))
        return alternatives, documents
    except LightGraphError as exc:
        result.issues.append({'table': table.qualified_name, 'code': exc.code, 'status': 'deferred'})
        return [], documents
    finally:
        tools.close()
        context.clear()
