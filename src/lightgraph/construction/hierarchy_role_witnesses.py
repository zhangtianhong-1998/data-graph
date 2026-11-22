"""Verify the role of a disconnected member from other members of its own source mappings."""
from collections import Counter, defaultdict


def _mapping_origins(node):
    by_key = defaultdict(set)
    for support in node['supports']:
        prov = support['provenance']
        key = tuple(prov['key_columns'])
        for origin in support.get('execution_origins') or [support.get('validation_origin') or {}]:
            definition = origin.get('definition', prov.get('definition'))
            number = origin.get('mapping', prov.get('mapping'))
            draft, revision, validation = origin.get('draft_id'), origin.get('draft_revision'), origin.get('validation_id')
            if (not definition or type(number) is not int or number < 0
                    or not (validation or (draft and type(revision) is int and revision >= 1))):
                return {}
            by_key[key].add((definition, number, draft, revision, validation))
    return by_key


def _mapping_role(node, member, keys, origins):
    # The label's *value* changes between members. Its physical source and
    # the projection's applicability, row context and properties must match.
    shape = frozenset((col, boundary, props) for _, col, _, boundary, props in member.bindings[keys])
    return node['type'], member.unit, member.address, keys, shape, frozenset(origins[keys])


class IsolatedRoleIndex:
    """A shared role needs the same independently observed, directed nonself edge in every projection.

    The edge must exercise the isolated node's own mapping at the same physical
    slot. It certifies what that slot represents; it never connects the isolated
    node to either endpoint. Different slots may have different witnesses.
    """

    def __init__(self, nodes, members, layouts, incidents, excluded):
        self.roles = {}
        self.anchors = {}
        self.common = {}
        self.components = {}
        self.layouts = {}
        self.units = {}
        self.unavailable = {}
        self.results = {}
        for nid, member in members.items():
            if incidents.get(nid) or nid in excluded or member.has_label_conflict:
                continue
            node = nodes[nid]
            origins = _mapping_origins(node)
            roles, positions = [], []
            for keys in sorted(member.bindings):
                layout = layouts.get((node['type'], member.address, keys))
                if not layout or len(keys) != 1:
                    self.unavailable[nid] = 'no_verified_level_layout'
                    break
                if not origins.get(keys):
                    self.unavailable[nid] = 'no_versioned_mapping'
                    break
                roles.append(_mapping_role(node, member, keys, origins))
                positions.append(tuple(sorted(layout, key=layout.get)))
            else:
                self.roles[nid] = tuple(roles)
                self.units[nid] = member.unit
                self.layouts[nid] = tuple(positions)
                for role in roles:
                    self.anchors.setdefault(role, set())
        if not self.anchors:
            return

        # A rejected, ambiguous or multiply-labelled endpoint cannot teach the
        # role of other members. No transitive equivalence through other units.
        endpoints = {}
        for nid, member in members.items():
            node_type = nodes[nid]['type']
            for binding in member.bindings:
                key = member.unit, node_type, member.address, binding, member.value
                good = nid not in excluded and not member.has_label_conflict
                endpoints[key] = good and key not in endpoints

        for nid, edges in incidents.items():
            if not edges or nid in excluded or members[nid].has_label_conflict:
                continue
            node, member = nodes[nid], members[nid]
            origins = _mapping_origins(node)
            if not origins:
                continue
            roles = {keys: _mapping_role(node, member, keys, origins) for keys in member.bindings}
            for parent, keys, other_keys, other_value, other_facts, edge_fact in edges:
                if member.value == other_value:
                    continue
                role = roles[keys]
                if role not in self.anchors:
                    continue
                local = member.unit, node['type'], member.address
                if not endpoints[(*local, keys, member.value)] or not endpoints.get((*local, other_keys, other_value)):
                    continue
                boundary, _ = edge_fact
                own_facts = member.bindings[keys]
                if any(fact[3] != boundary for fact in (*own_facts, *other_facts)):
                    continue
                own_names = frozenset((col, label) for _, col, label, _, _ in own_facts)
                self.anchors[role].add((parent, member.value, own_names, other_keys, other_value, other_facts, edge_fact))

    def _shared(self, roles):
        roles = frozenset(roles)
        if roles not in self.common:
            pools = sorted((self.anchors[r] for r in roles), key=len)
            self.common[roles] = any(all(anchor in pool for pool in pools[1:]) for anchor in pools[0])
        return self.common[roles]

    def _disjoint_roles(self, roles):
        """Find unrelated witness pools. Connectivity alone never authorizes a fold."""
        if roles not in self.components:
            by_anchor = defaultdict(list)
            for role in roles:
                for anchor in self.anchors[role]:
                    by_anchor[anchor].append(role)
            part = {}
            for role in roles:
                if role in part:
                    continue
                pending = [role]
                part[role] = role
                while pending:
                    for anchor in self.anchors[pending.pop()]:
                        for other in by_anchor.pop(anchor, ()):
                            if other not in part:
                                part[other] = role
                                pending.append(other)
            self.components[roles] = part
        return self.components[roles]

    def batches(self, ids):
        by_layout = defaultdict(set)
        for nid in ids:
            roles = self.roles.get(nid)
            if not roles:
                self.results[nid] = self.unavailable[nid]
            elif not all(self.anchors[r] for r in roles):
                self.results[nid] = 'no_mapping_witness_at_every_slot'
            else:
                by_layout[self.layouts[nid]].add(nid)
        for layout, batch in by_layout.items():
            if len(batch) == 1:
                self.results[next(iter(batch))] = 'no_eligible_peer_on_layout'
                continue
            batches = [batch]
            for slot in range(len(layout)):
                split = []
                for peers in batches:
                    roles = frozenset(self.roles[n][slot] for n in peers)
                    if self._shared(roles):
                        split.append(peers)
                    else:
                        part = self._disjoint_roles(roles)
                        separated = defaultdict(set)
                        for n in peers:
                            separated[part[self.roles[n][slot]]].add(n)
                        split.extend(separated.values())
                batches = split
            for peers in batches:
                if len(peers) < 2:
                    outcome = 'no_common_role_witness'
                elif len({self.units[n] for n in peers}) < 2:
                    outcome = 'same_unit_only'
                elif not all(self._shared(roles) for roles in zip(*(self.roles[i] for i in peers), strict=True)):
                    outcome = 'no_common_role_witness'
                else:
                    outcome = 'passed_role_check'
                self.results.update(dict.fromkeys(peers, outcome))
                if outcome == 'passed_role_check':
                    yield peers

    @property
    def diagnostics(self):
        """One outcome per queried isolated node, before ancestry and endpoint remap checks."""
        return dict(sorted(Counter(self.results.values()).items()))
