"""Actionable mapping advice; presentation quality never changes data validity."""
from collections import Counter

from .models import GraphDraft


def _primary_name_coverage(entity, index, nodes):
    """Count supported primary names, never promote aliases or cached labels.

    The caller must supply the graph visible to its audience. Mapping numbers
    and counts make the feedback actionable without retaining source values.
    """
    if not nodes or not any(getattr(mapping, 'label_column', None) or getattr(mapping, 'alias_columns', []) or getattr(mapping, 'display_columns', {})
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
        if any(getattr(mapping, 'kind', None) == 'metadata_column' for mapping in entity.mappings):
            continue
        has_primary_mapping = any(mapping.label_column for mapping in entity.mappings)
        for number, mapping in enumerate(entity.mappings):
            if len(mapping.key_columns) > 1 and mapping.label_column in mapping.key_columns:
                advice.append({
                    'code': 'primary_name_in_composite_key', 'severity': 'suggestion',
                    'entity_type': entity.type, 'path': ['entities', index, 'mappings', number, 'key_columns'],
                    'column': mapping.label_column, 'key_columns': list(mapping.key_columns),
                    'message': '主名称也是复合身份键的一部分。名称变更会生成另一个节点。',
                    'next_action': '核对业务键或唯一约束。若已有独立编码可识别对象，将名称映射到 label_column；'
                                   '若名称确实参与识别，保留完整复合键并记录依据。',
                })
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


def relation_evidence_advice(draft, graph=None):
    """Explain missing business scope and locators without changing raw observations."""
    from .contexts import business_context_identity
    from .relation_assertions import support_assertion_checks

    if not isinstance(draft, GraphDraft):
        draft = GraphDraft.model_validate(draft)
    advice = []
    for index, relation in enumerate(draft.relations):
        for number, mapping in enumerate(relation.mappings):
            if mapping.business_context.status == 'unknown':
                advice.append({'code': 'relation_business_scope_unknown', 'severity': 'suggestion',
                    'relation_type': relation.type, 'mapping': number,
                    'path': ['relations', index, 'mappings', number, 'business_context'],
                    'message': '关系尚未声明业务适用范围，原始观察会保留，但不能据此确认两条关系是同一事实。',
                    'next_action': '核对材料：全局关系显式声明 global；按项目、时期等限定的关系声明 scoped 并绑定对应列；无法确认时保留 unknown。'})
    counts = Counter()
    unknown_values = 0
    for edge in (graph or {}).get('edges', []):
        for support in edge.get('supports', []):
            counts.update(support_assertion_checks(support)['blockers'])
            mapping = (support.get('provenance') or {}).get('business_context_mapping') or {}
            unknown_values += mapping.get('status') == 'scoped' and business_context_identity(support)['status'] == 'unknown'
    if counts:
        advice.append({'code': 'relation_fact_comparison_incomplete', 'severity': 'suggestion',
            'blockers': dict(sorted(counts.items())), 'scoped_observations_with_unknown_values': unknown_values,
            'message': '部分关系证据不足以比较事实是否相同；各阻断项分别统计，数量可能重叠。原始关系和来源仍然保留。',
            'next_action': '补充所缺的适用范围或来源定位。查询投影凭证只标识已观察元组，不代表数据库物理行或跨源同一时刻快照。'})
    return advice
