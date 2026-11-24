"""Corroborate older, explicitly named hierarchy predicates with their physical mappings."""
import re


def _family(relation):
    return re.sub(r'__def_[0-9a-f]{32}$', '', relation.casefold())


# Older drafts sometimes expressed level roles in the predicate and key columns
# without recording hierarchy_orientation. Accept only adjacent levels of one key.
_CONTAINMENT = re.compile(r'\w+_l(\d{1,3})_in_l(\d{1,3})')
_PARENT = re.compile(r'(?P<kind>\w+)_(?:has_child|parent_of)_(?P=kind)|\w+_parent_child')
_KEY = re.compile(r'(\w+)_l(\d{1,3})_(code|id|key)', re.I)


def mapped_direction(relation):
    name = _family(relation)
    if _PARENT.fullmatch(name) or name in {'has_child', 'has_children', 'parent_of'}:
        return 'outgoing'
    if name in {'child_of', 'has_parent'}:
        return 'incoming'
    levels = _CONTAINMENT.fullmatch(name)
    if levels and int(levels[1]) == int(levels[2]) + 1:
        return 'incoming'
    return None


def _observed_level_pair(observation, flow, named_levels):
    if not isinstance(observation, dict) or not isinstance(observation.get('provenance'), dict):
        return None
    prov = observation['provenance']
    address = prov.get('source') or {}
    if (not isinstance(address, dict) or not all(isinstance(address.get(k), str) and address[k] for k in ('source_id', 'schema', 'table'))
            or observation.get('lookup_matches') or prov.get('source_lookup') or prov.get('target_lookup')):
        return None
    levels = []
    for field in ('source_key_columns', 'target_key_columns'):
        keys = prov.get(field)
        if not isinstance(keys, list) or len(keys) != 1 or not isinstance(keys[0], str):
            return None
        column = _KEY.fullmatch(keys[0])
        if not column:
            return None
        levels.append(column)
    src, dst = levels
    if (src[1] != dst[1] or src[3] != dst[3]
            or int(dst[2]) - int(src[2]) != (1 if flow == 'outgoing' else -1)
            or named_levels and (int(src[2]), int(dst[2])) != (int(named_levels[1]), int(named_levels[2]))):
        return None
    return address, src[0], dst[0]


def observed_level_pairs(relation, observations):
    flow = mapped_direction(relation)
    if not flow or not observations:
        return []
    named_levels = _CONTAINMENT.fullmatch(_family(relation))
    pairs = []
    for observation in observations:
        pair = _observed_level_pair(observation, flow, named_levels)
        if not pair:
            return []
        pairs.append(pair)
    return pairs


def bound_level_positions(source_column, target_column, context_columns):
    # Only peer key columns of the same named path may be ignored as extra path
    # coordinates when comparing row contexts. Keep every other column binding.
    path_key = _KEY.fullmatch(source_column)
    result = {}
    for column in (source_column, target_column, *context_columns):
        key = _KEY.fullmatch(column)
        if key and (key[1], key[3]) == (path_key[1], path_key[3]):
            result[column] = int(key[2])
    return result
