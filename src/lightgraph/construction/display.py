"""Derive readable labels from an already authorized graph, without changing IDs.

Always call this after filtering supports and edges for the viewer. Cached labels
are not evidence: an old artifact's fallback can include now-hidden information.
"""
from collections import Counter, defaultdict
from copy import deepcopy

from .fusion import annotate_visible_variants
from lightgraph.contracts import canonical
from lightgraph.source_values import identity_cell


def entity_name(node):
    """Choose a readable public name and preserve raw spellings in source observations."""
    for field in ('display_label', 'label', 'name'):
        text = node.get(field)
        if isinstance(text, str) and text.strip():
            return text.strip()
    return node.get('id', '')


def _value(value):
    return value if isinstance(value, str) else canonical(value)


def _parts(values):
    return '；'.join(f'{name}：{_value(value)}' for name, value in values.items())


def _identity_label(node):
    details = _parts(node.get('identity', {}))
    return f"{node['type']}（{details}）" if details else node['type']


def preferred_source_label(supports):
    """Return (name, priority) from visible source observations, in display order."""
    ranked = ((s['label_priority'], s['label']) for s in supports
              if isinstance(s.get('label_priority'), int) and s.get('label'))
    winner = min(ranked, default=None)
    if winner is not None:
        return winner[1], winner[0]
    return min((s['label'] for s in supports if s.get('label') not in (None, '')), default=None), None


def derive_display_graph(graph, *, copy=True):
    """Return a copy whose labels depend only on included supports and edges.

    Original source names remain exact. Missing names can use explicitly mapped
    display values, visible outgoing relationships, then the complete identity.
    Relation fallback is one pass over base labels, so cycles never recurse.
    Callers that already isolated the graph may set copy=False and allow label
    fields to be written in place.
    """
    result = deepcopy(graph) if copy else graph
    definitions = {item['type']: item for item in result.get('definitions', {}).get('entities', [])}
    by_id = {node['id']: node for node in result.get('nodes', [])}
    base = {}
    for node in by_id.values():
        supports = node.get('supports')
        if result.get('label_conflict_policy') == 'retain_variants' and supports is not None:
            annotate_visible_variants(node)
        # Legacy non-support fixtures have only a source label. Runtime graph
        # access rejects missing support metadata before reaching this function.
        preferred, _ = preferred_source_label(supports or [])
        if supports is None and node.get('label') and node.get('label_origin', 'source') == 'source':
            preferred = node['label']
        declared = defaultdict(list)
        for support in supports or []:
            for name, value in support.get('display_values', {}).items():
                if value is not None and canonical(value) not in {canonical(v) for v in declared[name]}:
                    declared[name].append(value)
        if preferred is not None:
            text, origin = preferred, 'source'
            label_supports = [s for s in supports or [] if s.get('label') == text]
            if label_supports and all(s.get('source_kind') in {'catalog', 'declared'} for s in label_supports):
                origin = 'metadata_declared' if any(s.get('source_kind') == 'declared' for s in label_supports) else 'metadata'
        elif declared:
            fields = {name: ' / '.join(_value(v) for v in sorted(values, key=canonical))
                      for name, values in sorted(declared.items())}
            text, origin = f"{node['type']}（{_parts(fields)}）", 'declared'
        else:
            text, origin = _identity_label(node), 'identity'
        base[node['id']] = (text, origin)

    # Distinct objects are allowed to share a source name. Disambiguate only
    # references inside generated labels, never rewrite the source names.
    names = Counter((node['type'], base[node['id']][0]) for node in by_id.values())
    outgoing = {
        node_id: [] for node_id, (_, origin) in base.items()
        if origin == 'identity' and definitions.get(by_id[node_id]['type'], {}).get('display_mode') != 'identity'
    }
    for edge in result.get('edges', []):
        if edge['source'] in outgoing and edge['target'] in by_id and edge['source'] != edge['target']:
            outgoing[edge['source']].append(edge)
    for node_id, node in by_id.items():
        text, origin = base[node_id]
        if outgoing.get(node_id):
            # Preserve the persisted name at every degree. Index membership by
            # text so a large set of dimensions does not need a quadratic scan.
            relation_pieces, covered = {}, set()
            for edge in sorted(outgoing[node_id], key=lambda e: (e['type'], base[e['target']][0], e['target'])):
                target = by_id[edge['target']]
                label = base[target['id']][0]
                if names[(target['type'], label)] > 1:
                    label += f"〔{_parts(target.get('identity', {}))}〕"
                relation_pieces[f"{edge['type']}：{label}"] = None
                for name in target.get('identity', {}):
                    if name in node.get('identity', {}) and identity_cell(node, name) == identity_cell(target, name):
                        covered.add(name)
            remaining = {name: value for name, value in node.get('identity', {}).items() if name not in covered}
            pieces = list(relation_pieces)
            if remaining:
                pieces.append(_parts(remaining))
            text, origin = f"{node['type']}（{'；'.join(pieces)}）", 'relations'
        node.update(label=text, display_label=text, label_origin=origin)
    return result
