"""Validate the prospective active graph, after replacing every supplied head."""
from collections import defaultdict
import json

from ..contracts import canonical
from ..errors import LightGraphError


def hierarchy_context(domain, qualifiers):
    """Display positions and permission flags do not create different trees."""
    return canonical([domain, {key: value for key, value in qualifiers.items()
        if key in {"taxonomy", "condition", "valid_period", "period", "hierarchy_version"}}])


def validate_hierarchy_rows(rows, *, rules=()):
    """Rows are support-authorized store rows from the complete prospective set.

    Multi-parent permission must be unanimous for the participating supports;
    one permissive rule cannot weaken another rule's tree constraint.
    """
    rules = {(rule.rule_id, rule.revision): rule for rule in rules}
    groups = defaultdict(lambda: {"edges": set(), "allow_dag": True})
    for row in rows:
        if row["fact_kind"] != "Edge" or row["status"] != "accepted":
            continue
        payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
        if payload["relation"] != "parent_of":
            continue
        qualifiers = payload.get("qualifiers", {})
        group = groups[hierarchy_context(row["isolation_domain"], qualifiers)]
        group["edges"].add((payload["source"], payload["target"]))
        rule = rules.get((row["rule_id"], row["revision"]))
        permission = (rule.hierarchy.allow_dag if rule and rule.hierarchy
                      else qualifiers.get("allow_dag", False))
        group["allow_dag"] &= permission is True
    from .engine import RuleEngine
    for group in groups.values():
        edges = group["edges"]
        if any(source == target for source, target in edges):
            raise LightGraphError("hierarchy_self_loop")
        if RuleEngine._cycles(edges):
            raise LightGraphError("hierarchy_cycle")
        parents = defaultdict(set)
        for source, target in edges:
            parents[target].add(source)
        if not group["allow_dag"] and any(len(values) > 1 for values in parents.values()):
            raise LightGraphError("multiple_parent")
