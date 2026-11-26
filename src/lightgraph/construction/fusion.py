"""Deterministic, explicit identity fusion and source-observation preservation."""
from copy import deepcopy
from collections import defaultdict
import unicodedata

from lightgraph.contracts import canonical, digest
from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import FusionPolicyConfig
from lightgraph.source_values import persisted_identity_key


def normalize_value(value, operations):
    if not operations:
        return value
    if not isinstance(value, str):
        raise LightGraphError('fusion_normalization_type_mismatch')
    for operation in operations:
        if operation == 'trim':
            value = value.strip()
        elif operation == 'casefold':
            value = value.casefold()
        elif operation == 'nfkc':
            value = unicodedata.normalize('NFKC', value)
    if not value:
        raise LightGraphError('fusion_empty_normalized_value')
    return value


def merge_property_values(existing, incoming, seen):
    """Append distinct typed observations in source order, indexing each value once."""
    for name, values in incoming.get('properties', {}).items():
        dest = existing.setdefault('properties', {}).setdefault(name, [])
        if name not in seen:
            seen[name] = {canonical(value) for value in dest}
        keys = seen[name]
        for value in values:
            key = canonical(value)
            if key not in keys:
                keys.add(key)
                dest.append(deepcopy(value))


def definition_contract(definition):
    """Compare meaning independently of presentation and internal variant names."""
    result = {k: deepcopy(v) for k, v in definition.items()
              if k not in {'description', 'display_mode', 'original_type', 'definition_variant',
                           'identity_evidence_refs'}}
    result['type'] = definition.get('original_type', definition['type'])
    if 'key_names' in result:
        result.setdefault('identity_origin', 'row')
        result.setdefault('identity_scope', 'unknown')
        result.setdefault('identity_context_keys', [])
        result.setdefault('identity_domain', None)
    elif 'source_type' in result and 'target_type' in result:
        parent = (definition.get('definition_variant') or {}).get('endpoint_projection')
        if parent and parent != digest(result):
            result['endpoint_projection'] = parent
    return result


def relation_identity(edge):
    """Retain business context when a definition or endpoint changes an edge ID."""
    from .contexts import business_context_identity
    context = business_context_identity(edge)
    parts = [edge['type'], edge['source'], edge['target']]
    if context.get('status') != 'unknown' or context.get('dimensions'):
        parts.append(context)
    return 'r_' + digest(*parts)


def edge_reference(edge):
    return {'edge_id': edge['id'], 'type': edge['type'], 'source': edge['source'], 'target': edge['target']}


def _add_endpoint_origin(support, origin):
    origins = support.setdefault('endpoint_origins', [])
    if isinstance(origins, dict):
        # Older observations can carry a single endpoint address.
        origins = support['endpoint_origins'] = [origins]
    if origin not in origins:
        origins.append(origin.copy())


def record_input_edge(edge, original, **owner):
    """Bind each rewritten support to the edge as supplied at this merge boundary."""
    if (edge['id'], edge['type'], edge['source'], edge['target']) == (
            original['edge_id'], original['type'], original['source'], original['target']):
        return
    origin = {**original, **owner}
    for support in edge.get('supports', ()):
        _add_endpoint_origin(support, origin)


def remap_graph_endpoints(graph, remap, *, self_contained, record_fusion=False):
    """Rewrite local references and keep inherited lookups in their base namespace."""
    if not remap:
        return
    for pending in graph.get('unresolved_relations', []):
        pending['candidate_endpoints'] = {
            end: sorted({remap.get(nid, nid) for nid in ids})
            for end, ids in pending.get('candidate_endpoints', {}).items()}
        pending['node_ids'] = sorted({remap.get(nid, nid) for nid in pending.get('node_ids', [])})
    relations = {rel['type']: rel for rel in graph.get('definitions', {}).get('relations', [])}
    for edge in graph.get('edges', []):
        old = (edge['source'], edge['target'])
        for end in ('source', 'target'):
            nid = edge[end]
            if nid not in remap or remap[nid] == nid:
                continue
            inherited = [match for support in edge.get('supports', []) for match in support.get('lookup_matches', [])
                         if match.get('endpoint') == end and match.get('inherited_node_id') == nid]
            if inherited and not self_contained:
                continue
            edge[end] = remap[nid]
            for match in inherited:
                match['inherited_node_id'] = edge[end]
        if old == (edge['source'], edge['target']):
            continue
        if record_fusion:
            if old[0] != old[1] and edge['source'] == edge['target'] and not relations.get(edge['type'], {}).get('allow_self_links'):
                raise LightGraphError('self_link_forbidden')
            for support in edge.get('supports', []):
                _add_endpoint_origin(support, {'edge_id': edge['id'], 'type': edge['type'],
                                               'source': old[0], 'target': old[1]})
        edge['id'] = relation_identity(edge)


def mark_relation_meanings(graphs):
    """Preserve distinct source explanations until the relation definitions have been aligned."""
    contracts = defaultdict(list)
    for graph in graphs:
        for relation in graph.get('definitions', {}).get('relations', []):
            # Already distinct endpoint contracts do not need a second qualifier
            # derived from wording. Keep their published structural fingerprints.
            structural = {k: v for k, v in relation.items() if k != 'relation_meaning_digest'}
            contracts[canonical(definition_contract(structural))].append(relation)
    for relations in contracts.values():
        descriptions = [' '.join((r.get('description') or '').split()) for r in relations]
        if len(set(descriptions) - {''}) > 1 or any('relation_meaning_digest' in r for r in relations):
            for relation, description in zip(relations, descriptions, strict=True):
                # A qualified publication still owns this meaning when its
                # display text is later abbreviated or omitted.
                relation.setdefault('relation_meaning_digest', digest(description))


def retain_definition_variants(graphs, *, self_contained=None, frozen_indices=()):
    """Keep incompatible contracts as stable local variants, preserving all facts.

    Mutates private graph copies only. Compatibility is deliberately conservative:
    a different key, scope, derivation or relation constraint needs alignment, not
    a guessed winning definition. Compatible families retain their ordinary type.
    """
    self_contained = self_contained or [True] * len(graphs)
    remaps = [{} for _ in graphs]
    for category, item_key in (('entities', 'nodes'), ('relations', 'edges')):
        if category == 'relations':
            mark_relation_meanings(graphs)
        families = defaultdict(set)
        already_qualified = set()
        name_owners = defaultdict(set)
        previous_names = defaultdict(set)
        frozen_owners = defaultdict(set)
        for index, graph in enumerate(graphs):
            for definition in graph.get('definitions', {}).get(category, []):
                contract = definition_contract(definition)
                family = contract['type']
                signature = digest(contract)
                families[family].add(signature)
                name_owners[definition['type']].add((family, signature))
                if index in frozen_indices:
                    frozen_owners[definition['type']].add((family, signature))
                if definition.get('definition_variant'):
                    already_qualified.add(family)
                    previous_names[(family, signature)].add(definition['type'])
        # A delta proposing a different contract must not take a frozen
        # publication's existing type name or change its node and edge IDs.
        # Multiple bases with competing claims keep the symmetric split.
        frozen_names = defaultdict(list)
        for name, owners in frozen_owners.items():
            if len(owners) == 1:
                owner, = owners
                frozen_names[owner].append(name)
                for other_family, _ in name_owners[name] - owners:
                    already_qualified.add(other_family)
        variant_names = {}
        for family, signatures in sorted(families.items()):
            if len(signatures) < 2 and family not in already_qualified:
                continue
            for signature in sorted(signatures):
                owner = (family, signature)
                if len(frozen_names[owner]) == 1:
                    variant_names[owner] = frozen_names[owner][0]
                    continue
                existing = sorted(name for name in previous_names[owner] if name_owners[name] == {owner})
                name = existing[0] if existing else f'{family[:60]}__def_{signature[:32]}'
                attempt = 0
                while name_owners.get(name, {owner}) != {owner}:
                    name = '__definition_' + digest('definition_namespace_v1', family, signature, attempt)
                    attempt += 1
                name_owners[name].add(owner)
                variant_names[owner] = name
        for index, graph in enumerate(graphs):
            aliases = {}
            for definition in graph.get('definitions', {}).get(category, []):
                contract = definition_contract(definition)
                family, signature = contract['type'], digest(contract)
                if len(families[family]) < 2 and family not in already_qualified:
                    continue
                old_type = definition['type']
                variant_type = variant_names[(family, signature)]
                variant = {'version': 1, 'contract_digest': signature}
                parent = (definition.get('definition_variant') or {}).get('endpoint_projection')
                if category == 'relations' and parent:
                    variant['endpoint_projection'] = parent
                aliases[old_type] = (variant_type, family, variant)
                definition.update(type=variant_type, original_type=family, definition_variant=variant)
            for item in graph.get(item_key, []):
                if item['type'] not in aliases:
                    continue
                previous_type = item['type']
                previous_variant = item.get('definition_variant', {})
                variant_type, family, variant = aliases[previous_type]
                item.update(type=variant_type, original_type=family, definition_variant=deepcopy(variant))
                if (category == 'entities' and previous_type != variant_type
                        and previous_variant.get('contract_digest') != variant['contract_digest']):
                    previous_id = item['id']
                    original = item.get('unit_original_id', previous_id)
                    variant_id = 'n_' + digest('definition_variant_v1', variant['contract_digest'], original)
                    if item.get('unit_id') and 'unit_original_id' in item:
                        item['unit_original_id'] = variant_id
                        item['id'] = 'n_' + digest('unit_scoped', item['unit_id'], variant_id)
                    else:
                        item['id'] = variant_id
                    remaps[index][previous_id] = item['id']
                elif category == 'relations' and previous_type != variant_type:
                    item['id'] = relation_identity(item)
            if category == 'relations':
                for pending in graph.get('unresolved_relations', []):
                    if pending.get('type') in aliases:
                        variant_type, family, variant = aliases[pending['type']]
                        pending.update(type=variant_type, original_type=family, definition_variant=deepcopy(variant))
            else:
                for definition in graph.get('definitions', {}).get('relations', []):
                    for endpoint in ('source_type', 'target_type'):
                        if definition.get(endpoint) in aliases:
                            definition[endpoint] = aliases[definition[endpoint]][0]
                remap_graph_endpoints(graph, remaps[index], self_contained=self_contained[index])
    for graph in graphs:
        for edge in graph.get('edges', []):
            if edge.get('business_context'):
                edge['id'] = relation_identity(edge)
    return remaps


def definition_conflict_diagnostics(graph):
    """Describe unresolved definition families using final, access-filterable IDs."""
    result = []
    for category, item_key in (('entities', 'nodes'), ('relations', 'edges')):
        families = defaultdict(set)
        contracts = defaultdict(set)
        for definition in graph.get('definitions', {}).get(category, []):
            if definition.get('definition_variant'):
                family = definition['original_type']
                families[family].add(definition['type'])
                parent = definition['definition_variant'].get('endpoint_projection') if category == 'relations' else None
                contracts[family].add(parent or digest(definition_contract(definition)))
        for family, variants in sorted(families.items()):
            if len(variants) < 2 or len(contracts[family]) < 2:
                continue
            affected = [item for item in graph.get(item_key, []) if item['type'] in variants]
            node_ids = ({item['id'] for item in affected} if category == 'entities'
                        else {item[end] for item in affected for end in ('source', 'target')})
            edge_ids = ({edge['id'] for edge in graph.get('edges', [])
                         if edge['source'] in node_ids or edge['target'] in node_ids}
                        if category == 'entities' else {item['id'] for item in affected})
            result.append({'status': 'unresolved', 'reason': 'incompatible_definition_contracts',
                           'category': category, 'original_type': family,
                           'definition_ids': sorted(variants), 'node_ids': sorted(node_ids),
                           'edge_ids': sorted(edge_ids)})
    return result


def merge_definition(previous, incoming):
    """Only identity/relationship contracts block; display text is not identity."""
    if previous is None:
        result = deepcopy(incoming)
        if 'key_names' in result:
            for name, default in (('identity_origin', 'row'), ('identity_scope', 'unknown'),
                                  ('identity_context_keys', []), ('identity_domain', None)):
                result.setdefault(name, default)
        return result
    if definition_contract(previous) != definition_contract(incoming):
        raise LightGraphError('published_definition_conflict')
    result = deepcopy(previous)
    if 'identity_evidence_refs' in previous or 'identity_evidence_refs' in incoming:
        result['identity_evidence_refs'] = sorted(set(previous.get('identity_evidence_refs', []))
                                                  | set(incoming.get('identity_evidence_refs', [])))
    descriptions = sorted({d.get('description', '') for d in (previous, incoming)} - {''})
    if descriptions:
        result['description'] = descriptions[0]
    if 'display_mode' in previous or 'display_mode' in incoming:
        modes = {d.get('display_mode', 'auto') for d in (previous, incoming)}
        result['display_mode'] = next(iter(modes)) if len(modes) == 1 else 'auto'
    return result


def _property_observations(properties, rule, source):
    prefix = (f"{source['source_id']}.{source.get('schema') or 'public'}.{source['table']}."
              if source.get('source_id') and source.get('table') else '')
    for name, values in sorted(properties.items()):
        target = rule.property_aliases.get(prefix + name, rule.property_aliases.get(name, name))
        for raw in values:
            yield {'property': target, 'value': normalize_value(raw, rule.property_normalizers.get(target, [])),
                   'original_property': name, 'raw_value': raw}


def _property_values(observations):
    result = {}
    for observation in observations:
        value = observation['value']
        result.setdefault(observation['property'], {})[canonical(value)] = value
    return {name: [bucket[key] for key in sorted(bucket)] for name, bucket in sorted(result.items())}


def _check_unrecorded_fusion(graph, rules, target_types, previous_targets):
    """Require raw contributions when a new rule would reinterpret an old fused key."""
    if not (graph.get('fusion_policy_digest') or graph.get('fusion_policy_version')):
        return
    for node in graph.get('nodes', []):
        family = node.get('original_type', node['type'])
        domain = node.get('identity_domain')
        if not domain or family in previous_targets:
            continue
        origins = (origin for support in node.get('supports', []) for origin in support.get('identity_origins', []))
        if node['type'] not in target_types and family not in target_types and not any(
                (origin.get('type'), frozenset(origin.get('identity', {}))) in rules for origin in origins):
            continue
        # A declared identity domain alone does not mean this node was fused.
        # The generated ID distinguishes explicit fusion from source-backed aliases.
        fused_id = 'n_' + digest('fusion_v1', domain, family, persisted_identity_key(node))
        stored_id = node.get('unit_original_id', node['id'])
        signature = node.get('definition_variant', {}).get('contract_digest')
        if stored_id == fused_id or (signature and stored_id == 'n_' + digest('definition_variant_v1', signature, fused_id)):
            raise LightGraphError('fusion_policy_requires_original_contributions')


def _only_adds_unobserved_property_mappings(graph, old, new):
    """An incremental rule may name a new table's attributes without reinterpreting the base."""
    fields = {'property_aliases', 'property_normalizers'}
    if {k: v for k, v in old.items() if k not in fields} != {k: v for k, v in new.items() if k not in fields}:
        return False
    if any(new[field].get(k) != v for field in fields for k, v in old.get(field, {}).items()):
        return False
    old_aliases = old.get('property_aliases', {})
    new_aliases = new['property_aliases']
    added_normalizers = new['property_normalizers'].keys() - old.get('property_normalizers', {}).keys()
    other_inputs = {(rule['entity_type'], frozenset(rule['key_mapping'].values()))
                    for rule in graph.get('fusion_policy', {}).get('entity_rules', ())
                    if (rule['canonical_type'], rule['identity_domain']) == (old['canonical_type'], old['identity_domain'])}
    other_inputs.discard((old['entity_type'], frozenset(old['key_mapping'].values())))

    def from_other_rule(origin):
        return (isinstance(origin, dict) and origin.get('id') and isinstance(origin.get('identity'), dict)
                and (origin.get('type'), frozenset(origin['identity'])) in other_inputs)

    for node in graph.get('nodes', ()):
        if (old['canonical_type'] not in (node['type'], node.get('original_type'))
                or node.get('identity_domain') != old['identity_domain']):
            continue
        if not node.get('supports'):
            return False
        observed = set()
        for support in node['supports']:
            observed.update((key, canonical(value)) for key, values in support.get('properties', {}).items()
                            for value in values)
            # Several rules may feed one canonical type. A field name can mean
            # something different for another original type or key contract.
            origins = support.get('identity_origins') or ()
            if origins and all(from_other_rule(origin) for origin in origins):
                continue
            source = (support.get('provenance') or {}).get('source') or {}
            if not source.get('source_id') or not source.get('table'):
                return False
            prefix = f"{source['source_id']}.{source.get('schema') or 'public'}.{source['table']}."
            names = set(support.get('observed_properties', support.get('properties', {})))
            if 'observed_properties' not in support:
                # A legacy support may contain only canonical names. Check every
                # possible prior source name for the properties that survived.
                stored = names.copy()
                names.update(key.removeprefix(prefix) for key, target in old_aliases.items() if target in stored)
            for name in names:
                previous = old_aliases.get(prefix + name, old_aliases.get(name, name))
                incoming = new_aliases.get(prefix + name, new_aliases.get(name, name))
                if previous != incoming or incoming in added_normalizers:
                    return False
        if any((key, canonical(value)) not in observed for key, values in node.get('properties', {}).items()
               for value in values):
            return False
    return True


def prepare_graph(graph, policy, *, copy=True, self_contained=None, local_namespace=True):
    """Return a new graph with explicit type/key mapping and rewritten endpoints.

    No key may be dropped, and physical-column identities remain source-specific.
    Original identities/properties are retained within their authorized supports.
    """
    policy = policy if isinstance(policy, FusionPolicyConfig) else FusionPolicyConfig.model_validate(policy)
    result = deepcopy(graph) if copy else graph
    policy_snapshot = policy.model_dump(mode='json')
    policy_digest = digest(policy_snapshot)
    rules = {(r.entity_type, frozenset(r.key_mapping.values())): r for r in policy.entity_rules}
    target_types = {r.entity_type: r.canonical_type for r in policy.entity_rules}
    previous = result.get('fusion_policy', {})
    # Older prepare_graph outputs stored only the digest of the applied policy.
    if not previous and result.get('fusion_policy_digest') == policy_digest:
        previous = policy_snapshot
    previous_targets = {rule['canonical_type'] for rule in previous.get('entity_rules', [])}
    _check_unrecorded_fusion(result, rules, target_types, previous_targets)
    from .fusion_bindings import validate_source_bindings
    validate_source_bindings(result, rules, previous_targets, local_namespace=local_namespace)
    for old in previous.get('entity_rules', []):
        new = rules.get((old['entity_type'], frozenset(old['key_mapping'].values())))
        if not new:
            continue
        if old.get('source_bindings') and not new.source_bindings:
            raise LightGraphError('fusion_policy_requires_original_contributions')
        # All current bindings, including newly restricted historical sources,
        # have already been checked per observation.
        snapshot = new.model_dump(mode='json', exclude={'source_bindings'})
        old = {k: v for k, v in old.items() if k != 'source_bindings'}
        if snapshot != old and not _only_adds_unobserved_property_mappings(result, old, snapshot):
            # A canonical node can already represent several original identities.
            # Re-keying it requires its pre-fusion contributions and edge witnesses.
            raise LightGraphError('fusion_policy_requires_original_contributions')

    def rule_for(entity_type, keys):
        rule = rules.get((entity_type, frozenset(keys)))
        if rule is None and entity_type in target_types:
            raise LightGraphError('fusion_identity_keys_mismatch')
        return rule

    canonical_rules = {rule.canonical_type: rule for rule in policy.entity_rules}
    remap = {}
    raw_ids = [node['id'] for node in result.get('nodes', [])]
    original_key_names = {d['type']: list(d['key_names']) for d in result.get('definitions', {}).get('entities', [])}

    def prepared(item, keys):
        if not item.get('identity_domain') or item['type'] not in previous_targets:
            return False
        expected = canonical_rules.get(item['type'])
        if expected is None and item['type'] in target_types:
            raise LightGraphError('fusion_policy_requires_original_contributions')
        if expected is not None and (item['identity_domain'] != expected.identity_domain
                                     or set(keys) != set(expected.key_mapping)):
            raise LightGraphError('fusion_identity_keys_mismatch')
        return True

    for definition in result.get('definitions', {}).get('entities', []):
        if prepared(definition, definition['key_names']):
            continue
        rule = rule_for(definition['type'], definition['key_names'])
        if rule is None:
            continue
        if definition.get('identity_origin', 'row') != 'row':
            raise LightGraphError('fusion_physical_identity_forbidden')
        if definition.get('identity_domain') not in (None, rule.identity_domain):
            raise LightGraphError('fusion_identity_domain_conflict')
        if definition.get('identity_context_keys'):
            key_aliases = {source: target for target, source in rule.key_mapping.items()}
            definition['identity_context_keys'] = [key_aliases[key] for key in definition['identity_context_keys']]
        definition.update(type=rule.canonical_type, key_names=sorted(rule.key_mapping), identity_domain=rule.identity_domain)
    for definition in result.get('definitions', {}).get('relations', []):
        for end in ('source_type', 'target_type'):
            if definition.get(end) in target_types:
                definition[end] = target_types[definition[end]]
    for node in result.get('nodes', []):
        if prepared(node, node['identity']):
            continue
        rule = rule_for(node['type'], node['identity'])
        if rule is None:
            continue
        if node.get('identity_origin', 'row') != 'row':
            raise LightGraphError('fusion_physical_identity_forbidden')
        if node.get('identity_domain') not in (None, rule.identity_domain):
            raise LightGraphError('fusion_identity_domain_conflict')
        original = {key: deepcopy(node[key]) for key in ('id', 'type', 'identity', 'identity_types') if key in node}
        if rule.source_bindings:
            original['source_key_names'] = list(original_key_names[node['type']])
        if any(value is None for value in original['identity'].values()):
            raise LightGraphError('fusion_missing_identity_value')
        if any(rule.key_normalizers.get(key) and original.get('identity_types', {}).get(source, 'text') != 'text'
               for key, source in rule.key_mapping.items()):
            raise LightGraphError('fusion_normalization_type_mismatch')
        node['identity'] = {key: normalize_value(original['identity'][source], rule.key_normalizers.get(key, []))
                            for key, source in sorted(rule.key_mapping.items())}
        node['identity_types'] = {key: original.get('identity_types', {})[source]
            for key, source in sorted(rule.key_mapping.items()) if source in original.get('identity_types', {})}
        node.update(type=rule.canonical_type, identity_domain=rule.identity_domain)
        node['id'] = 'n_' + digest('fusion_v1', rule.identity_domain, rule.canonical_type, persisted_identity_key(node))
        remap[original['id']] = node['id']
        observations = []
        for support in node.get('supports', []):
            physical = support.get('provenance', {}).get('key_columns', [])
            names = original_key_names.get(original['type'], [])
            if physical and len(names) == len(physical):
                support['original_key_values'] = {col: original['identity'][name] for col, name in zip(physical, names)}
            if original not in support.setdefault('identity_origins', []):
                support['identity_origins'].append(original)
            if rule.property_aliases or rule.property_normalizers:
                support.setdefault('observed_properties', deepcopy(support.get('properties', {})))
                fields = _property_observations(support['observed_properties'], rule,
                                                support.get('provenance', {}).get('source', {}))
                support['property_observations'] = list(fields)
                observations.extend(support['property_observations'])
                support['properties'] = _property_values(support['property_observations'])
        if rule.property_aliases or rule.property_normalizers:
            if not node.get('supports'):
                observations = _property_observations(node.get('properties', {}), rule, {})
            node['properties'] = _property_values(observations)
    if len(set(remap.values())) < len(remap):
        from .observed_identity import contradictory_qualifiers
        members = defaultdict(list)
        for raw_id, node in zip(raw_ids, result.get('nodes', []), strict=True):
            members[node['id']].append((raw_id, node))
        for target, occurrences in members.items():
            if len(occurrences) > 1 and contradictory_qualifiers(node for _, node in occurrences):
                for original, node in occurrences:
                    remap[original] = node['id'] = 'n_' + digest('fusion_qualifier_conflict', target, original)
    if self_contained is None:
        self_contained = not result.get('extends_base_graph')
    remap_graph_endpoints(result, remap, self_contained=self_contained, record_fusion=True)
    result['label_conflict_policy'] = policy.label_conflict_policy
    result['fusion_policy_version'] = policy.version
    result['fusion_policy'] = policy_snapshot
    result['fusion_policy_digest'] = policy_digest
    return result


def stamp_execution(item, graph):
    """Tie each observation to its source validation, including repeated draft runs."""
    stamp = {k: graph[k] for k in ('draft_id', 'draft_revision', 'validation_id') if k in graph}
    if stamp:
        for support in item.get('supports', []):
            support['validation_origin'] = stamp


def label_priority(support, authority_sources):
    """Match an explicitly preferred source, table, or original name column."""
    prov = support.get('provenance', {})
    src = prov.get('source', {})
    source_id = src.get('source_id')
    if not source_id:
        return None
    names = {source_id}
    if src.get('table'):
        table = '.'.join([source_id, src.get('schema') or 'public', src['table']])
        names.add(table)
        if prov.get('label_column'):
            names.add(table + '.' + prov['label_column'])
    return next((idx for idx, name in enumerate(authority_sources) if name in names), None)


def deduplicate_supports(item):
    """Count source observations once, retaining every extraction execution.

    Scope, source columns, row context, raw and normalized values identify an
    observation. Execution provenance records each independent extraction.
    """
    if 'identity' in item:
        owner = [item['type'], item.get('identity_domain'), persisted_identity_key(item)]
    else:
        owner = [item['type'], item.get('source'), item.get('target')]
    collected = {}
    for support in item.get('supports', []):
        provenance = support.get('provenance', {})
        execution = {key: support[key] for key in ('contributor_id',) if key in support}
        execution.update(support.get('validation_origin', {}))
        execution.update({key: provenance[key] for key in ('definition', 'mapping') if key in provenance})
        refs = sorted(set(support.get('evidence_refs', [])) | set(provenance.get('evidence_refs', [])))
        if refs:
            execution['evidence_refs'] = refs
        observed = dict(support)
        for key in ('contributor_id', 'evidence_refs', 'execution_origins', 'observation_id', 'identity_origins', 'validation_origin', 'label_authority', 'label_priority'):
            observed.pop(key, None)
        # A checkpoint address is lineage of an observation, not data from its row.
        # Ignore the newly attached input references when identifying observations;
        # retain the original behavior of legacy, unqualified endpoint histories.
        if isinstance(observed.get('endpoint_origins'), list):
            observed['endpoint_origins'] = [{k: v for k, v in o.items() if k != 'stage'}
                                           for o in observed['endpoint_origins'] if 'input_kind' not in o]
            if not observed['endpoint_origins']:
                observed.pop('endpoint_origins')
        # A normalizer can map two raw codes in one table to a single key.
        # Their raw source rows are separate observations even when their names agree.
        raw_keys = [origin.get('identity') for origin in support.get('identity_origins', [])]
        if raw_keys and 'original_key_values' not in observed:
            observed['original_identities'] = sorted(raw_keys, key=canonical)
        if 'provenance' in observed:
            observed['provenance'] = dict(provenance)
            for key in ('definition', 'mapping', 'evidence_refs'):
                observed['provenance'].pop(key, None)
        observation = 'obs_' + digest(owner, observed)
        support.pop('validation_origin', None)
        target = collected.setdefault(observation, {**support, 'observation_id': observation, 'execution_origins': []})
        for origin in support.get('execution_origins') or ([execution] if execution else []):
            if origin not in target['execution_origins']:
                target['execution_origins'].append(origin)
        for origin in support.get('identity_origins', []):
            if origin not in target.setdefault('identity_origins', []):
                target['identity_origins'].append(origin)
        if isinstance(support.get('endpoint_origins'), list):
            for origin in support['endpoint_origins']:
                _add_endpoint_origin(target, origin)
        for where in (None, 'provenance'):
            src = support if where is None else support.get(where, {})
            if 'evidence_refs' in src:
                dst = target if where is None else target.setdefault(where, {})
                dst['evidence_refs'] = sorted(set(dst.get('evidence_refs', [])) | set(src['evidence_refs']))
    for target in collected.values():
        target['execution_origins'].sort(key=canonical)
        if 'identity_origins' in target:
            target['identity_origins'].sort(key=canonical)
    item['supports'] = [collected[key] for key in sorted(collected)]


def annotate_visible_variants(node):
    """Derived presentation only: invoke after filtering supports for visibility."""
    variants = {}
    for support in node.get('supports', []):
        label = support.get('observed_label', support.get('label'))
        if label is not None:
            variants.setdefault(str(label), set()).add(support.get('scope_ref', 'public'))
    node['label_variants'] = sorted(variants)
    by_scope = {}
    for label, scopes in variants.items():
        for scope in scopes:
            by_scope.setdefault(scope, set()).add(label)
    node['label_conflict'] = any(len(labels) > 1 for labels in by_scope.values())
