"""Shared search budgets and evidence projection for graph paths."""
from lightgraph.contracts import canonical


# Search limits are separate from the size of the answer. A wide frontier or
# many incompatible row witnesses must not use an unbounded number of SQL calls.
MAX_EXPANSIONS = 128
MAX_WITNESS_PAIRS = 8192
MAX_STATES = 2048
FANOUT = 48


def evidence_preview(witness):
    """Large attribute maps and execution histories are available via exact-ID lookup."""
    if len(canonical(witness).encode('utf-8')) <= 2048:
        return witness
    refs = witness.get('evidence_refs') or []
    return {'projection': 'reference_only', 'observation_id': witness.get('observation_id'),
            'label': str(witness.get('label') or '')[:256], 'business_context': witness.get('business_context'),
            'evidence_refs': refs[:8], 'evidence_ref_count': len(refs),
            'source_tables': (witness.get('source_tables') or [])[:8]}


def path_node(node, witness, slots, is_seed):
    # The index's display name/description may already aggregate several
    # authorized business contexts. Render the particular witness used here.
    return {'id': node['id'], 'kind': node['kind'], 'identity': node['identity'],
            'display_name': witness.get('label') or node['id'],
            'source_tables': witness.get('source_tables', []), 'description': witness.get('description'),
            'observations': [witness], 'is_seed': is_seed, 'matched_slots': slots, 'is_bridge': False}
