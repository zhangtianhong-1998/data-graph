"""Corroborate older, explicitly named hierarchy predicates with their physical mappings."""
import json
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


def corroborated_level_keys(relation, observations_json):
    """A SQLite cohort row contributes evidence only when all its bindings agree."""
    flow = mapped_direction(relation)
    if not flow:
        return False
    named_levels = _CONTAINMENT.fullmatch(_family(relation))
    observations = json.loads(observations_json)
    if not observations:
        return False
    for obs in observations:
        prov = obs.get('provenance') or {}
        address = prov.get('source') or {}
        if (not all(isinstance(address.get(k), str) and address[k] for k in ('source_id', 'schema', 'table'))
                or obs.get('lookup_matches') or prov.get('source_lookup') or prov.get('target_lookup')):
            return False
        levels = []
        for field in ('source_key_columns', 'target_key_columns'):
            keys = prov.get(field)
            if not isinstance(keys, list) or len(keys) != 1 or not isinstance(keys[0], str):
                return False
            column = _KEY.fullmatch(keys[0])
            if not column:
                return False
            levels.append(column)
        src, dst = levels
        if (src[1] != dst[1] or src[3] != dst[3]
                or int(dst[2]) - int(src[2]) != (1 if flow == 'outgoing' else -1)
                or named_levels and (int(src[2]), int(dst[2])) != (int(named_levels[1]), int(named_levels[2]))):
            return False
    return True
