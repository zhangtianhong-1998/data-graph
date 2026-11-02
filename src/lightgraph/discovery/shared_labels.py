"""Verify redundant name columns against an already shared identity domain.

Column names only select a possible mapping. Full key/name aggregates establish
equality; the normal rule execution and publication gates still own acceptance.
No source rows or values are retained by this helper.
"""
from lightgraph.contracts import SourceRef, canonical
from lightgraph.discovery.goal import column_is_excluded, column_is_evidence_only, source_in_goal
from lightgraph.errors import LightGraphError


def _shared(rule):
    identity = rule.identity
    evidence = rule.evidence
    return (rule.kind == 'column_group_entity'
        and identity.semantic_resolution == 'verified_identity_domain'
        and identity.identity_scope == 'domain_global'
        and not rule.scope.namespaces and identity.attribute_evidence
        and (evidence.origin == 'user_rule' or
             evidence.semantic_status == 'supported' and evidence.conditions_satisfied
             and evidence.semantic_decision in {'explicit_declaration', 'model_review'}
             and not evidence.refuting_refs))


def _allowed(goal, table, columns):
    from .tools import _SENSITIVE
    return (source_in_goal(goal, table) and set(columns) <= set(table.column_names)
        and all(not _SENSITIVE.search(c) and not column_is_excluded(goal, table, c)
                and not column_is_evidence_only(goal, table, c) for c in columns))


def enrich_shared_labels(*, table, tables, adapter, goal, rules, validated_rules,
                         remaining, execute):
    """Add exact redundant labels to existing rules, never create identities.

    Only same-adapter, global domains with identical key arity/normalization are
    eligible. Missing, conflicting or ambiguous values remain unexplained. Query
    errors retain their operational status and stop this table's follow-up.
    """
    from .coverage import attribute_enrichment
    source = SourceRef(source_id=table.source_id, schema=table.schema_name, table=table.name)
    catalog = {SourceRef(source_id=t.source_id, schema=t.schema_name, table=t.name).model_dump_json(): t
               for t in tables}
    issues = []; stats = {}; attempts = 0
    for previous in list(validated_rules):
        if (previous.source != source or not _shared(previous)
                or 'label' in previous.attributes.single_valued
                or 'label' in previous.attributes.multi_valued):
            continue
        donors = []
        for donor in validated_rules:
            label = donor.attributes.single_valued.get('label')
            if (donor.source == source or donor.source.source_id != source.source_id
                    or not _shared(donor) or label not in remaining
                    or donor.identity.entity_kind != previous.identity.entity_kind
                    or donor.identity.identity_domain != previous.identity.identity_domain
                    or donor.normalization != previous.normalization
                    or len(donor.identity.key_columns) != len(previous.identity.key_columns)):
                continue
            target = catalog.get(donor.source.model_dump_json())
            if (target is None or label in previous.identity.key_columns
                    or label in {e.column for e in previous.attributes.excluded}
                    or not _allowed(goal, table, [*previous.identity.key_columns, label])
                    or not _allowed(goal, target, [*donor.identity.key_columns, label])):
                continue
            donors.append((donor, target, label))
        # Competing label interpretations need ordinary review, not first-match
        # preference based on table order.
        if not donors or len(donors) > 8 or len({label for _, _, label in donors}) != 1:
            continue
        if attempts >= 32:
            break
        attempts += 1
        label = donors[0][2]
        matched = True
        try:
            with adapter.snapshot():
                for rule, target in [(previous, table), *[(d, t) for d, t, _ in donors]]:
                    fd = adapter.check_fd(target, rule.identity.key_columns, [label], rule.scope.predicate)
                    if (not fd.get('x_groups') or any(fd.get(name, 1) != 0 for name in
                            ('violating_groups', 'all_null_groups', 'mixed_null_groups', 'null_key_rows'))):
                        matched = False
                        break
                if matched:
                    for donor, target, _ in donors:
                        counts = adapter.check_reference(table, [*previous.identity.key_columns, label],
                            target, [*donor.identity.key_columns, label],
                            predicate=previous.scope.predicate, target_predicate=donor.scope.predicate)
                        if (not counts.get('source_ref_tuples') or any(counts.get(name, 1) != 0
                                for name in ('null_refs', 'unmatched', 'ambiguous'))
                                or counts.get('unique_matches') != counts['source_ref_tuples']):
                            matched = False
                            break
        except LightGraphError as exc:
            issues.append({'table': table.qualified_name, 'code': exc.code, 'status': 'deferred'})
            return issues, stats, True
        if not matched:
            issues.append({'table': table.qualified_name, 'code': 'coverage_shared_label_unresolved',
                           'column_names': canonical([label]), 'status': 'unresolved'})
            continue
        candidate = previous.model_copy(deep=True)
        candidate.attributes.single_valued['label'] = label
        enriched = attribute_enrichment(previous, candidate)
        if enriched is None:
            continue
        enriched.dependencies.documents = sorted(set(previous.dependencies.documents).union(
            *(set(d.dependencies.documents) for d, _, _ in donors)))
        if execute(enriched, preserve_previous=True):
            rules[:] = [r for r in rules if r.rule_id != previous.rule_id] + [enriched]
            # Pipeline execute owns this list, but standalone callers can supply
            # the same validation callback without a separate list mutation.
            validated_rules[:] = [r for r in validated_rules if r.rule_id != previous.rule_id] + [enriched]
            stats['coverage_shared_label_revisions'] = stats.get('coverage_shared_label_revisions', 0) + 1
            stats['coverage_shared_label_columns'] = stats.get('coverage_shared_label_columns', 0) + 1
        else:
            issues.append({'table': table.qualified_name, 'code': 'coverage_shared_label_execution_failed',
                           'status': 'deferred'})
            return issues, stats, True
    return issues, stats, False
