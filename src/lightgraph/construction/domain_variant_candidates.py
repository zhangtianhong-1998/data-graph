"""Offer individual domain variants for review from their visible source observations."""
from collections import defaultdict

from lightgraph.contracts import canonical
from lightgraph.source_values import identity_cell
from .fusion import definition_contract
from .hierarchy_observations import _predicate_terms, observation_scope
from .identity_authority import source_address


_WORKSPACE_OWNER = object()


def _contracts(graph):
    """Index only families differing in domain, retaining the entire remaining contract."""
    variants = defaultdict(dict)
    for definition in graph.get('definitions', {}).get('entities', []):
        if (not definition.get('definition_variant') or not definition.get('original_type')
                or definition.get('identity_scope') != 'global' or definition.get('identity_context_keys')
                or definition.get('identity_origin', 'row') != 'row' or not definition.get('identity_domain')):
            continue
        names = definition.get('key_names') or []
        if names and len(set(names)) == len(names):
            contract = definition_contract(definition)
            domain = contract.pop('identity_domain')
            variants[canonical(contract)][definition['type']] = (domain, tuple(names))
    return {kind: (contract, domain, names) for contract, family in variants.items()
            if len({domain for domain, _ in family.values()}) > 1
            for kind, (domain, names) in family.items()}


def _source_observation(support, names, identity, types, local_workspace):
    """Use the raw typed key only on its original, locally owned observation."""
    provenance = support.get('provenance') or {}
    source = source_address(provenance.get('source'))
    columns = provenance.get('key_columns') or []
    label_column = provenance.get('label_column')
    label = support.get('observed_label') or support.get('label')
    owner = support.get('contributor_id')
    origins = support.get('execution_origins') or []
    if local_workspace and 'contributor_id' not in support and all('contributor_id' not in o for o in origins):
        owner = _WORKSPACE_OWNER
    context = support.get('business_context') or {}
    if (not owner or any(origin.get('contributor_id', _WORKSPACE_OWNER) != owner for origin in origins)
            or support.get('inherited_from') or support.get('lookup_matches') or not support.get('scope_ref')
            or context.get('missing_dimensions') or context.get('conflicts')):
        return None
    if (support.get('identity_basis') or support.get('identity_origins') or support.get('identity_decision_refs')
            or 'original_key_values' in support):
        return None
    if (not source or len(columns) != len(names) or len(set(columns)) != len(columns)
            or not all(isinstance(c, str) and c for c in columns)
            or not label_column or not isinstance(label, str) or not label.strip()):
        return None
    row = support.get('row_context') or {}
    values, row_types = row.get('values') or {}, row.get('value_types') or {}
    if any(col in values and (row_types.get(col) != types[key] or values[col] != identity[key])
           for key, col in zip(names, columns, strict=True)):
        return None
    if label_column in values and str(values[label_column]) != label:
        return None
    predicate = _predicate_terms(provenance.get('predicate'), {*columns, label_column})
    return (owner, source, tuple(columns), label_column, label, observation_scope(support),
            predicate, canonical(provenance.get('constant_properties') or {}))


def domain_variant_candidates(visible_graph, *, local_workspace=False):
    """Find pairs in visible evidence. Only a caller with a verified local workspace may bind untagged supports."""
    contracts = _contracts(visible_graph)
    matches = defaultdict(dict)
    for node in visible_graph.get('nodes', []):
        kind = node.get('type')
        if kind not in contracts:
            continue
        contract, domain, names = contracts[kind]
        identity, types = node.get('identity') or {}, node.get('identity_types') or {}
        identity_context = node.get('identity_context')
        if identity_context is not None and (identity_context.get('status') != 'global'
                                             or identity_context.get('dimensions')):
            continue
        if (domain != node.get('identity_domain') or node.get('identity_origin', 'row') != 'row'
                or set(names) != identity.keys() or set(names) != types.keys()
                or any(identity[k] is None or identity[k] == '' for k in names)):
            continue
        cells = tuple(identity_cell(node, k) for k in names)
        if any(cell[0] != types[k] for k, cell in zip(names, cells, strict=True)):
            continue
        observations = frozenset(_source_observation(s, names, identity, types, local_workspace)
                                 for s in node.get('supports', []))
        if not observations or None in observations or len({obs[0] for obs in observations}) != 1:
            continue
        matches[contract, cells, observations][node['id']] = domain
    pairs = sorted(sorted(nodes) for nodes in matches.values()
                   if len(nodes) == 2 and len(set(nodes.values())) == 2)
    return [{'status': 'candidate_only', 'reason': 'same_source_key_different_identity_domains', 'node_ids': ids}
            for ids in pairs]
