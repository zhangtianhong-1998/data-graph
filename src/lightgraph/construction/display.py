"""Derive readable labels from an already authorized graph, without changing IDs.

Always call this after filtering supports and edges for the viewer. Cached labels
are not evidence: an old artifact's fallback can include now-hidden information.
"""
from collections import Counter, defaultdict
from copy import deepcopy

from lightgraph.contracts import canonical
from lightgraph.source_values import identity_cell


def _value(value):
    return value if isinstance(value, str) else canonical(value)


def _parts(values):
    return '；'.join(f'{name}：{_value(value)}' for name, value in values.items())


def _identity_label(node):
    details = _parts(node.get('identity', {}))
    return f"{node['type']}（{details}）" if details else node['type']


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
        # Legacy non-support fixtures have only a source label. Runtime graph
        # access rejects missing support metadata before reaching this function.
        labels = sorted({support['label'] for support in supports or [] if support.get('label') not in (None, '')})
        if supports is None and node.get('label') and node.get('label_origin', 'source') == 'source':
            labels = [node['label']]
        declared = defaultdict(list)
        for support in supports or []:
            for name, value in support.get('display_values', {}).items():
                if value is not None and canonical(value) not in {canonical(v) for v in declared[name]}:
                    declared[name].append(value)
        if labels:
            text, origin = labels[0], 'source'
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
    outgoing = defaultdict(list)
    for edge in result.get('edges', []):
        if edge['source'] in by_id and edge['target'] in by_id and edge['source'] != edge['target']:
            outgoing[edge['source']].append(edge)
    for node_id, node in by_id.items():
        text, origin = base[node_id]
        mode = definitions.get(node['type'], {}).get('display_mode', 'auto')
        if origin == 'identity' and mode != 'identity' and outgoing[node_id]:
            pieces, covered = [], set()
            for edge in sorted(outgoing[node_id], key=lambda e: (e['type'], base[e['target']][0], e['target'])):
                target = by_id[edge['target']]
                label = base[target['id']][0]
                if names[(target['type'], label)] > 1:
                    label += f"〔{_parts(target.get('identity', {}))}〕"
                piece = f"{edge['type']}：{label}"
                if piece not in pieces:
                    pieces.append(piece)
                for name, value in target.get('identity', {}).items():
                    if name in node.get('identity', {}) and identity_cell(node, name) == identity_cell(target, name):
                        covered.add(name)
            remaining = {name: value for name, value in node.get('identity', {}).items() if name not in covered}
            if remaining:
                pieces.append(_parts(remaining))
            text, origin = f"{node['type']}（{'；'.join(pieces)}）", 'relations'
        node.update(label=text, display_label=text, label_origin=origin)
    return result
