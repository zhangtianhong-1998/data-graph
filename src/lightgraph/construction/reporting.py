"""Build factual run summaries without treating narrative suggestions as failures."""
from __future__ import annotations

from lightgraph.contracts import ConstructionSummary
from .statistics import graph_statistics


def draft_is_ready(record, publications, *, discover_only=False):
    """A selected snapshot can be published after an unrelated failed attempt."""
    published = publications.get(record.draft.draft_id, {})
    if (not discover_only and record.status == 'published'
            and published.get('revision') == record.revision
            and published.get('draft_fingerprint') == record.fingerprint
            and published.get('context_fingerprint') == record.context_fingerprint):
        return True
    checked = record.validation
    if (record.status not in {'validated', 'published'} or checked is None or not checked.passed
            or checked.revision != record.revision or checked.draft_fingerprint != record.fingerprint
            or checked.context_fingerprint != record.context_fingerprint):
        return False
    return discover_only or publications.get(record.draft.draft_id, {}).get('revision') == record.revision


def construction_summary(records, publications, graph, *, execution_kind='not_run'):
    drafts = []
    for record in records:
        draft = record.draft
        checked = record.validation
        drafts.append({'draft_id': draft.draft_id, 'revision': record.revision, 'status': record.status,
            'entity_types': len(draft.entities), 'relation_types': len(draft.relations),
            'mappings': sum(len(item.mappings) for item in draft.entities + draft.relations),
            'unresolved': draft.unresolved, 'decisions': draft.decisions, 'suggestions': draft.suggestions,
            'validation': {'passed': checked.passed if checked else None,
                'metrics': checked.metrics if checked else {}, 'diagnostics': checked.diagnostics if checked else []}})
    checks = [record.validation for record in records]
    data_validation = ('not_run' if not any(checks) else
                      'passed' if checks and all(check and check.passed for check in checks) else 'failed')
    stats = graph_statistics(graph)
    return ConstructionSummary(drafts=drafts,
        summary={'draft_count': len(drafts), 'published_drafts': len(publications),
                 'entity_type_count': stats['entity_type_count'], 'relation_type_count': stats['relation_type_count'],
                 'mapping_count': sum(stats['observed_mappings'].values())},
        execution={'model_runtime': execution_kind, 'data_validation': data_validation}, statistics=stats)


def draft_issues(records, publications, *, discover_only=False):
    """Legacy unresolved strings remain blockers until explicitly reclassified."""
    issues = []
    for record in records:
        draft = record.draft
        for category, values in [('goal_gap', draft.unresolved), ('decision', draft.decisions),
                                 ('suggestion', draft.suggestions)]:
            for value in values:
                issues.append({'code': 'construction_' + category, 'category': category,
                    'status': 'unresolved' if category == 'goal_gap' else 'diagnostic',
                    'draft_id': draft.draft_id, 'message': value})
        if not draft_is_ready(record, publications, discover_only=discover_only):
            code = ('construction_draft_validation_failed' if record.validation and not record.validation.passed
                    else 'construction_unpublished_draft')
            issues.append({'code': code, 'status': 'unresolved',
                           'draft_id': draft.draft_id})
    return issues
