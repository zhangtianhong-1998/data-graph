"""Actionable mapping advice; presentation quality never changes data validity."""
from .models import GraphDraft


def name_mapping_advice(draft, graph=None):
    """Ask for explicit name roles without guessing from physical column names.

    A property-only mapping may legitimately enrich an entity named elsewhere.
    Unnamed associations may also intentionally use generated display labels.
    These observations are advice, not failed validation or inferred semantics.
    """
    if not isinstance(draft, GraphDraft):
        draft = GraphDraft.model_validate(draft)
    advice = []
    for index, entity in enumerate(draft.entities):
        if any(mapping.label_column for mapping in entity.mappings):
            continue
        for number, mapping in enumerate(entity.mappings):
            if not mapping.display_columns:
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
    return advice
