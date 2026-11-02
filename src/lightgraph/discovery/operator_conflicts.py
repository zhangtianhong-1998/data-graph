"""Retain competing operator mappings as value-free, reviewable work."""
from lightgraph.contracts import digest
from .conflicts import candidate_dependency, rule_mapping
from .models import PendingCandidate


def context_conflict_candidate(rule, competing, *, table, goal=None, context_version=''):
    """Keep the proposed mapping and bounded accepted alternatives, never source rows."""
    alternatives = sorted(competing, key=lambda item: (item.rule_id, item.revision))
    mappings = [rule_mapping(item, table, goal=goal) for item in [rule, *alternatives[:5]]]
    refs = sorted({item.rule_id for item in [rule, *alternatives]})
    return PendingCandidate(
        candidate_id='candidate_' + digest('operator_identity', rule.source.model_dump(),
            sorted((item.rule_id, item.revision) for item in [rule, *alternatives]))[:24],
        source=rule.source, proposal_kind='column_group' if rule.kind == 'column_group_entity' else rule.kind,
        failure_code='conflicting_rule_proposals', review_stage='concept_mapping',
        column_refs=sorted({column for mapping in mappings for field in mapping.fields for column in field.columns}),
        rule_refs=refs[:64], mappings=mappings, differences=['identity_policy','other_contract'],
        dependency_fingerprint=candidate_dependency(table, goal, context_version))
