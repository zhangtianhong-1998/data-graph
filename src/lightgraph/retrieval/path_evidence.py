"""Shared search budgets and evidence projection for graph paths."""
from lightgraph.contracts import canonical
from .store_sqlite import normalize_term


# Search limits are separate from the size of the answer. A wide frontier or
# many incompatible row witnesses must not use an unbounded number of SQL calls.
MAX_EXPANSIONS = 128
MAX_WITNESS_PAIRS = 8192
MAX_STATES = 2048
FANOUT = 48
BINDING_FIELDS = ('columns', 'key_columns', 'label_column', 'property_columns', 'source_key_columns',
                  'target_key_columns', 'scope_columns', 'predicate', 'context_columns', 'constant_properties',
                  'business_context_mapping', 'source_lookup', 'target_lookup')


def evidence_preview(witness):
    """Large attribute maps and execution histories are available via exact-ID lookup."""
    if len(canonical(witness).encode('utf-8')) <= 2048:
        return witness
    refs = witness.get('evidence_refs') or []
    preview = {'projection': 'reference_only', 'observation_id': witness.get('observation_id'),
               'label': str(witness.get('label') or '')[:256], 'business_context': witness.get('business_context'),
               'evidence_refs': refs[:8], 'evidence_ref_count': len(refs),
               'source_tables': (witness.get('source_tables') or [])[:8]}
    if witness.get('identity_decision_refs'):
        preview['identity_reviewed'] = True
    # An agent can use a large observation's physical binding independently of
    # its attribute map. Keep the predicate and keys together or omit the binding.
    provenance = witness.get('provenance')
    source = provenance.get('source') if isinstance(provenance, dict) else None
    if isinstance(source, dict) and source.get('table'):
        binding = {key: provenance[key] for key in BINDING_FIELDS if key in provenance}
        binding['source'] = {key: source[key] for key in ('source_id', 'schema', 'table') if key in source}
        mapped = {**preview, 'projection': 'source_binding', 'provenance': binding}
        if witness.get('source_identity'):
            with_key = {**mapped, 'source_identity': witness['source_identity']}
            if len(canonical(with_key).encode('utf-8')) <= 2048:
                return with_key
        if len(canonical(mapped).encode('utf-8')) <= 2048:
            return mapped
    return preview


def path_node(node, witness, slots, is_seed):
    # The index's display name/description may already aggregate several
    # authorized business contexts. Render the particular witness used here.
    return {'id': node['id'], 'kind': node['kind'], 'identity': node['identity'],
            'display_name': witness.get('label') or node['id'],
            'source_tables': witness.get('source_tables', []), 'description': witness.get('description'),
            'observations': [witness], 'is_seed': is_seed, 'matched_slots': slots, 'is_bridge': False}


def witness_names(node):
    for obs in node['observations']:
        for value in (obs.get('label'), obs.get('english_name'), *(obs.get('aliases') or [])):
            if isinstance(value, str):
                yield normalize_term(value)


def _witness_codes(node, observation):
    if observation.get('identity_decision_refs') or observation.get('identity_reviewed'):
        return (str(v) for v in (observation.get('source_identity') or {}).get('values', ()))
    return node.get('identity', {}).values()


def matches_operand(node, term):
    """Only a name on the selected witness or a typed identity can ground this operand."""
    return (term in witness_names(node)
            or any(normalize_term(value) == term for obs in node['observations'] for value in _witness_codes(node, obs)))


def grounds_slot(node, observation, slot):
    """Tie a textual operand to the particular authorized name or code used in a proof."""
    match = slot.get('match') or slot.get('source') or 'exact'
    if match == 'node_id':
        return True
    candidate = next((c for c in slot['candidates'] if c['node_id'] == node['id']), {})
    terms = [slot.get('text') or slot.get('surface')]
    if match == 'semantic':
        terms = candidate.get('matched_terms') or []
    if match == 'assumption':
        terms = [candidate.get('display_name')]
    names = {normalize_term(v) for v in (
        observation.get('label'), observation.get('english_name'), *(observation.get('aliases') or ()),
        *_witness_codes(node, observation)) if v is not None}
    names.discard('')
    return any(normalize_term(term) in names for term in terms)


def _merge_proofs(collection, incoming):
    for identity, item in incoming.items():
        existing = collection.get(identity)
        if existing is None:
            collection[identity] = item
        else:
            for field in ('source_tables', 'evidence_refs', 'matched_slots'):
                if field in item:
                    existing[field] = sorted(set(existing[field]) | set(item[field]))
            for obs in item['observations']:
                if obs not in existing['observations']:
                    existing['observations'].append(obs)


class PathCollection:
    """Keep whole, independently verified paths within a shared response budget."""

    def __init__(self, request):
        self.max_nodes = request.max_nodes
        self.max_edges = request.max_edges
        self.nodes = {}
        self.edges = {}
        self.truncation = set()
        self.components = []

    def add(self, proof):
        nodes = {n['id']: n for n in proof['nodes']}
        edges = {e['id']: e for e in proof['edges']}
        if len(self.nodes.keys() | nodes.keys()) > self.max_nodes or len(self.edges.keys() | edges.keys()) > self.max_edges:
            self.truncation.add('output_limit')
            return False
        _merge_proofs(self.nodes, nodes)
        _merge_proofs(self.edges, edges)
        joined = set(nodes)
        for component in self.components[:]:
            if component & joined:
                joined.update(component)
                self.components.remove(component)
        self.components.append(joined)
        return True
