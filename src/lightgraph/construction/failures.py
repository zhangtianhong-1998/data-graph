"""Separate source execution failures from counterexamples to a mapping."""

SOURCE_FAILURES = frozenset({
    'read_only_transaction_failed', 'snapshot_close_failed', 'source_closed',
    'database_connection_failed', 'database_reconnect_failed', 'source_catalog_changed',
    'query_execution_failed', 'query_timeout', 'query_cancel_failed',
    'query_cancelled', 'construction_source_execution_failed',
    'mapping_source_unavailable', 'relation_dependency_unavailable',
    'snapshot_or_projection_unavailable', 'runtime_capability_unverified',
})


def failure_guidance(diagnostics):
    codes = {d.get('code') for d in diagnostics}
    if 'memory_budget_exhausted' in codes:
        return {'failure_class': 'resource', 'retryable': False,
                'next_action': 'Keep the full mapping and its requested names and row contexts. '
                    'Memory exhaustion is not a semantic counterexample. Save unresolved work; '
                    'retry after an approved capacity change or split independent drafts without '
                    'dropping dependencies or claiming the reduced goal is complete.'}
    if codes & SOURCE_FAILURES:
        source_errors = [d for d in diagnostics if d.get('code') in SOURCE_FAILURES]
        retryable = any(d.get('retryable') is True for d in source_errors)
        reconnect = any(d.get('database', {}).get('recovery') == 'reconnect_required' for d in source_errors)
        if codes & {'database_reconnect_failed', 'source_catalog_changed'} or any(
                d.get('source_blocked') is True for d in source_errors):
            return {'failure_class': 'source_execution', 'retryable': False,
                    'recovery': 'operator_requalification_required',
                    'next_action': 'Automatic reconnection or catalog requalification did not succeed. '
                        'Do not retry unchanged or discard the original mappings. Preserve the failed-source '
                        'requirements, continue independent work, and report that the operator must restore '
                        'and requalify this source before a new complete validation.'}
        if any(d.get('new_snapshot_recovery_available') is True for d in source_errors):
            return {'failure_class': 'source_execution', 'retryable': False,
                    'recovery': 'new_snapshot_available',
                    'next_action': 'The failed snapshot is closed and the adapter can attempt one '
                        'qualified reconnection before a NEW independent snapshot. Preserve the draft; '
                        'you may start one fresh complete validation or an independent read within the '
                        'remaining budget. This does not authorize replaying or joining partial old results. '
                        'If recovery or the same operation fails again, stop unchanged retries and retain '
                        'the unresolved source requirements. Do not delete names, dimensions or path context.'}
        if 'query_timeout' in codes and not reconnect:
            return {'failure_class': 'query_timeout', 'retryable': False,
                    'recovery': 'inspect_query_scope',
                    'next_action': 'Preserve the mappings, names and path context. A query timeout alone '
                        'does not prove a disconnected source or invalid semantics. Do not repeat the same '
                        'expensive query unchanged. Inspect the stage and connection diagnostic; use existing '
                        'evidence or a narrower goal-relevant probe, recording any changed range. GROUP BY '
                        'or DISTINCT with LIMIT may still scan the full table. Samples cannot replace '
                        'complete instance validation. Stop if no justified lower-cost probe is available.'}
        return {'failure_class': 'source_execution', 'retryable': retryable,
                'recovery': 'reconnect_and_requalify' if reconnect else 'inspect_diagnostic',
                'next_action': 'Keep the source mappings, labels and complete path context. '
                    'A failed source operation does not prove the mapping is wrong or access is permanently forbidden. '
                    'Inspect the source_id, phase and database category; unknown is not proof of timeout. '
                    'Only retry when marked retryable and within the remaining budget. A closed/disconnected '
                    'source requires a newly qualified connection. Healthy-source results are candidates; '
                    'publish independent drafts only after their own validation. Preserve the original unmet goal.'}
    if 'same_identity_conflicting_label' in codes:
        return {'failure_class': 'data_conflict', 'retryable': False,
                'next_action': 'If publishable_subset is present, publish_graph_draft can publish the '
                    'verified remainder while quarantining conflicting identities and their incident edges. '
                    'This preserves every original counterexample and leaves goal gaps unresolved. '
                    'Read conflicts:<draft_id> for retained keys, names and source mappings even when the database is unavailable; '
                    'the material identifies the observed revision and cannot validate a new revision. '
                    'Check names for the same entity type, full identity key and access scope. '
                    'Different codes sharing a name and one identity occurring in multiple positions are allowed. '
                    'Do not move conflicting names to aliases/display fields or filter away records solely to pass. '
                    'Read the saved draft and validation:<draft_id> material, then inspect only the affected '
                    'key and name columns. A conflict alone does not prove a missing responsibility-center key: '
                    'check whether differing names occur inside one scope, across scopes, or reflect a wrong column binding. '
                    'Do not add physical hierarchy positions to identity just to pass. '
                    'Resolve canonical-name versus alternate-name semantics using evidence, or preserve the conflict.'}
    return {}
