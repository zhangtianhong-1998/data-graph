"""Revisit a failed source selection once when the frozen index shows a concrete alternative."""
import time

from .overlap_review import review_overlap_patterns
from .projection_proposals import verify_projection_proposals


def _alternative(pattern, alignment):
    selected = {part['member_index']: part['source_index'] for part in alignment}
    for row in pattern.get('binding_comparisons', {}).get('rows', []):
        members = row['member_indexes']
        if (all(i in selected for i in members) and row['matching_observed_names'] > 0
                and list(row['source_indexes']) != [selected[i] for i in members]):
            return True
    return False


def reconsider_source_selections(config, node, inputs, patterns, result, policy, checks, *,
                                 model_api_key, check_cancelled, on_model_call, deadline):
    """A failed alternative cannot displace a verified rule or authorize a new member."""
    by_id = {p['pattern_id']: p for p in patterns}
    decisions = {d['pattern_id']: d for d in result['decisions']}
    previous = {}
    cards = []
    for check in checks:
        if check['verified_groups'] or check['reason'] not in {'no_shared_keys', 'selected_binding_has_no_observations'}:
            continue
        pid = check['pattern_id']
        decision = decisions[pid]
        if _alternative(by_id[pid], decision['alignment']):
            previous[pid] = {'decision': decision, 'check': check}
            cards.append({**by_id[pid], 'previous_check': {'alignment': decision['alignment'], 'reason': check['reason']}})
    if not cards:
        return policy, checks

    limits = [n for n in (node.max_calls, config.resolved_model.max_calls) if n is not None]
    remaining = max(0, min(limits) - result['model_calls']) if limits else None
    followup = review_overlap_patterns(config, cards, api_key=model_api_key, max_calls=remaining,
                                       remaining_seconds=deadline - time.monotonic(), check_cancelled=check_cancelled,
                                       on_model_call=on_model_call)
    check_cancelled()
    revised = [d for d in followup['decisions'] if d['action'] == 'propose' and d != decisions[d['pattern_id']]]
    policy, rechecked = verify_projection_proposals(policy, inputs, cards, revised)
    outcomes = {row['pattern_id']: row for row in rechecked}
    # Keep the effective decisions replayable. The separate history preserves both
    # the failed selection and the model's reply (including keep, review or failure).
    withdrawn = set()
    for decision in followup['decisions']:
        pid = decision['pattern_id']
        decisions[pid] = decision
        if decision['action'] in ('keep', 'review'):
            withdrawn.add(pid)
    result['decisions'] = list(decisions.values())
    result['source_selection_review'] = {'prior': previous, 'review': followup}
    result['model_calls'] += followup['model_calls']
    batches = result.setdefault('batches', [])
    offset = len(batches)
    for batch in followup.get('batches', []):
        parent = batch.get('parent_attempt')
        batches.append({**batch, 'phase': 'reconsider_source_selection', 'attempt': batch['attempt'] + offset,
                        'parent_attempt': parent + offset if parent is not None else None})
    for field in ('truncated_batches', 'retry_batches', 'expanded_output_retries'):
        result[field] = result.get(field, 0) + followup.get(field, 0)
    recovered = {pid for batch in batches if batch.get('parent_attempt') and batch['status'] in ('partial', 'reviewed')
                 for pid in batch['pattern_ids'] if pid not in batch.get('invalid_proposals', {})}
    result['recovered_patterns'] = len(recovered)
    return policy, [outcomes.get(row['pattern_id'], row) for row in checks if row['pattern_id'] not in withdrawn]
