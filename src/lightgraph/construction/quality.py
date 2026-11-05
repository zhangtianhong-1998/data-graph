"""Actionable mapping advice; presentation quality never changes data validity."""
from collections import Counter

from .models import GraphDraft


def _primary_name_coverage(entity, index, nodes):
    """Count supported primary names, never promote aliases or cached labels.

    The caller must supply the graph visible to its audience. Mapping numbers
    and counts make the feedback actionable without retaining source values.
    """
    if not nodes or not any(mapping.label_column or mapping.alias_columns or mapping.display_columns
                            for mapping in entity.mappings):
        return None
    missing, alias_only, display_only = 0, 0, 0
    affected = Counter()
    for node in nodes:
        supports = node.get('supports', [])
        if any(isinstance(support.get('label'), str) and support['label'].strip()
               for support in supports):
            continue
        missing += 1
        alias_only += any(support.get('aliases') for support in supports)
        display_only += any(support.get('display_values') for support in supports)
        indexes = {support.get('provenance', {}).get('mapping') for support in supports
                   if support.get('provenance', {}).get('definition') == entity.type}
        for number in indexes:
            if isinstance(number, int) and 0 <= number < len(entity.mappings):
                affected[number] += 1
    if not missing:
        return None
    return {
        'code': 'primary_name_coverage_incomplete', 'severity': 'suggestion',
        'entity_type': entity.type, 'path': ['entities', index, 'mappings'],
        'observed_nodes': len(nodes), 'nodes_with_primary_name': len(nodes) - missing,
        'nodes_without_primary_name': missing, 'unnamed_nodes_with_aliases': alias_only,
        'unnamed_nodes_with_display_values': display_only,
        'mapping_coverage': [
            {'mapping': number, 'path': ['entities', index, 'mappings', number, 'label_column'],
             'nodes_without_primary_name': count,
             'label_column': entity.mappings[number].label_column,
             'alias_columns': entity.mappings[number].alias_columns,
             'display_columns': sorted(set(entity.mappings[number].display_columns.values()))}
            for number, count in sorted(affected.items())],
        'message': '部分已观察到的对象没有主名称支持；其他映射设置了名称列，不代表这些对象也已获得名称。'
                   '别名和组合展示不会自动成为主名称。',
        'next_action': '检查所列映射的名称角色及名称列空值，为缺名对象补充经确认的主名称映射。'
                       '不要把真实名称冲突改成别名来绕过检查；确实没有独立名称的对象可以保留现状。',
    }


def name_mapping_advice(draft, graph=None):
    """Ask for explicit name roles without guessing from physical column names.

    A property-only mapping may legitimately enrich an entity named elsewhere.
    Unnamed associations may also intentionally use generated display labels.
    These observations are advice, not failed validation or inferred semantics.
    If provided, graph must already be filtered to the caller's visible scopes.
    """
    if not isinstance(draft, GraphDraft):
        draft = GraphDraft.model_validate(draft)
    advice = []
    nodes_by_type = {}
    for node in (graph or {}).get('nodes', []):
        nodes_by_type.setdefault(node.get('type'), []).append(node)
    for index, entity in enumerate(draft.entities):
        has_primary_mapping = any(mapping.label_column for mapping in entity.mappings)
        for number, mapping in enumerate(entity.mappings):
            if has_primary_mapping or not mapping.display_columns:
                continue
            advice.append({
                'code': 'primary_name_mapping_review', 'severity': 'suggestion',
                'entity_type': entity.type,
                'path': ['entities', index, 'mappings', number, 'label_column'],
                'display_columns': sorted(set(mapping.display_columns.values())),
                'message': '此对象没有主名称映射，展示字段会生成组合标签，也不会自动成为别名。',
                'next_action': '若已有可靠名称列，请设置 label_column；经确认的其他名称放入 alias_columns。'
                               '若对象确实没有独立名称，可保留组合展示并在 decisions 中说明。',
            })
        coverage = _primary_name_coverage(entity, index, nodes_by_type.get(entity.type, []))
        if coverage is not None:
            advice.append(coverage)
    return advice
