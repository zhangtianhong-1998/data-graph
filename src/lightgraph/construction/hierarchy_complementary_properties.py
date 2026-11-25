"""Identify compatible source attributes omitted by a peer's hierarchy mapping."""
from collections import defaultdict

from lightgraph.contracts import canonical
from .contexts import support_business_context
from .observed_identity import identity_qualifier


def complementary_columns(nodes, members, physical_groups):
    """Compare only projections of the same typed key, physical slot and exact source name.

    An omitted property adds no competing identity evidence. A mapped-but-empty
    cell, a value conflict, or a use of this column as a condition remains binding.
    No observation is removed from the published supports.
    """
    result = defaultdict(dict)
    supplies_properties = {nid for nid in members if any(s['provenance'].get('property_columns')
                                                         for s in nodes[nid]['supports'])}
    if not supplies_properties:
        return result
    hierarchy_columns = defaultdict(set)
    known_applicability = set()
    for nid, member in members.items():
        columns = hierarchy_columns[nodes[nid]['type'], member.address]
        for keys, column, _, _, _ in member.observations:
            columns.update(keys)
            columns.add(column)
        if all(support_business_context(s)['status'] in ('scoped', 'global') for s in nodes[nid]['supports']):
            known_applicability.add(nid)
    for (address, keys, value), ids in physical_groups.items():
        if (len(ids) < 2 or supplies_properties.isdisjoint(ids)
                or any(physical_groups[address, slot, value] != ids for nid in ids for slot in members[nid].bindings)):
            continue
        cohorts = defaultdict(set)
        for nid in ids & known_applicability:
            member = members[nid]
            if member.has_label_conflict:
                continue
            bindings = member.bindings[keys]
            scopes = frozenset(fact[3].scope for fact in bindings)
            for _, col, label, _, _ in bindings:
                cohorts[col, label, scopes].add(nid)
        for (name_column, _, _), peers in cohorts.items():
            if len(peers) < 2:
                continue
            if any(boundary.predicate for nid in peers
                   for _, col, _, boundary, _ in members[nid].bindings[keys] if col == name_column):
                # An explicit applicability filter keeps the original comparison
                # of every mapped property. Key/non-empty-name guards were already
                # verified and normalized at the source observation boundary.
                continue
            values, owners, alias_sources = defaultdict(set), defaultdict(set), defaultdict(set)
            protected = set()
            for nid in peers:
                protected.update(hierarchy_columns[nodes[nid]['type'], address])
                for support in nodes[nid]['supports']:
                    prov = support['provenance']
                    if tuple(prov['key_columns']) != keys or prov['label_column'] != name_column:
                        continue
                    protected.update(prov.get('context_columns') or ())
                    protected.update(prov.get('scope_columns') or ())
                    protected.update(prov.get('alias_columns') or ())
                    protected.update((prov.get('english_name_column'), prov.get('description_column')))
                    protected.update((support.get('display_columns') or {}).values())
                    context = prov.get('business_context_mapping') or {}
                    protected.update((context.get('columns') or {}).values())
                    for alias, column in (prov.get('property_columns') or {}).items():
                        alias_sources[alias].add(column)
                        observations = (support.get('properties') or {}).get(alias)
                        if (identity_qualifier(prov, alias) or alias in (prov.get('hierarchy_position_roles') or {})
                                or column not in (prov.get('columns') or ())
                                or alias in (prov.get('constant_properties') or {})
                                or not isinstance(observations, list) or len(observations) != 1
                                or not isinstance(observations[0], (str, float, int)) or observations[0] == ''):
                            protected.add(column)
                            continue
                        owners[column].add(nid)
                        values[column].add(canonical(observations[0]))
            for sources in alias_sources.values():
                if len(sources) > 1:
                    protected.update(sources)
            optional = {c for c in values.keys() - protected if len(values[c]) == 1 and owners[c] != peers}
            if optional:
                for nid in peers:
                    result[nid][keys, name_column] = optional
    return result
