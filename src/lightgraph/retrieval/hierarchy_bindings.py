"""SQLite predicates for the strict hierarchy reader."""
import json

from lightgraph.hierarchy_bindings import mapped_direction, observed_level_pairs


def corroborated_level_keys(relation, observations_json):
    """SQLite predicate used by strict one-hop hierarchy queries."""
    return bool(observed_level_pairs(relation, json.loads(observations_json)))
