"""Nonblocking feedback when a saved revision drops declared graph coverage."""
from collections import defaultdict

from .models import GraphDraft, iter_source_mappings


_REVIEW_ACTION = (
    '上一版草稿仍保存在修订历史中。请确认这些变化符合用户调整后的范围；'
    '不要仅因查询、内存或其他执行失败删去仍需完成的业务内容。'
    '合法的范围调整可以保留，本提示不判定目标未完成。'
)


def _source(mapping):
    return mapping.source_id, mapping.schema_name, mapping.table


def _source_ref(source):
    return dict(zip(('source_id', 'schema', 'table'), source))


def _mapping_key(mapping):
    if getattr(mapping, 'kind', None) == 'metadata_column':
        return _source(mapping) + ('metadata_column', mapping.column)
    keys = ((tuple(mapping.key_columns),) if hasattr(mapping, 'label_column') else
            (tuple(mapping.source_key_columns), tuple(mapping.target_key_columns)))
    if hasattr(mapping, 'source_metadata'):
        keys += tuple((name, ref.source_id, ref.schema_name, ref.table, ref.column)
            for name in ('source_metadata', 'target_metadata') if (ref := getattr(mapping, name)) is not None)
    return _source(mapping) + keys


def _mapping_ref(mapping):
    result = {'source': _source_ref(_source(mapping))}
    if getattr(mapping, 'kind', None) == 'metadata_column':
        return {**result, 'column': mapping.column, 'source_kind': 'metadata_column'}
    names = ('key_columns',) if hasattr(mapping, 'label_column') else ('source_key_columns', 'target_key_columns')
    result.update({name: list(getattr(mapping, name)) for name in names})
    return result


def revision_loss_advice(previous_draft, new_draft):
    """Compare declarations after saving a revision, without judging intent.

    Source/column/type references are returned, never predicate values, property
    constants, labels from data or free-text decisions. Mapping order is not an
    identity: matching uses its physical source and ordered identity/endpoints.
    Compatible mappings may supply a former name or context from another index.
    The workspace retains previous revisions; this function makes no mutations.
    """
    if previous_draft is None:
        return []
    previous = previous_draft if isinstance(previous_draft, GraphDraft) else GraphDraft.model_validate(previous_draft)
    current = new_draft if isinstance(new_draft, GraphDraft) else GraphDraft.model_validate(new_draft)
    advice = []

    def add(code, message, **details):
        advice.append({'code': code, 'severity': 'suggestion', **details,
                       'message': message, 'next_action': _REVIEW_ACTION})

    before_sources = {_source(mapping) for mapping in iter_source_mappings(previous)}
    after_sources = {_source(mapping) for mapping in iter_source_mappings(current)}
    removed_sources = before_sources - after_sources
    if removed_sources:
        add('revision_sources_removed', '部分原有来源表已不在本版草稿中。',
            source_ids=sorted({source[0] for source in before_sources} - {source[0] for source in after_sources}),
            tables=[_source_ref(source) for source in sorted(removed_sources)])

    for collection, kind in (('entities', 'entity'), ('relations', 'relation')):
        before = getattr(previous, collection)
        after = {definition.type: (index, definition) for index, definition in enumerate(getattr(current, collection))}
        removed = sorted({definition.type for definition in before} - set(after))
        if removed:
            add(f'revision_{collection}_removed', '部分原有对象或关系定义已从本版草稿移除。',
                definition_kind=kind, removed_types=removed, path=[collection])
        for previous_index, definition in enumerate(before):
            if definition.type not in after:
                continue
            current_index, new_definition = after[definition.type]
            candidates = defaultdict(list)
            for number, mapping in enumerate(new_definition.mappings):
                candidates[_mapping_key(mapping)].append((number, mapping))
            for number, mapping in enumerate(definition.mappings):
                matches = candidates[_mapping_key(mapping)]
                location = {'definition_kind': kind, 'definition_type': definition.type,
                    'previous_path': [collection, previous_index, 'mappings', number],
                    **_mapping_ref(mapping)}
                if not matches:
                    add('revision_mappings_removed', '原有物理字段映射已不在本版草稿中。', **location)
                    continue
                location['matching_paths'] = [[collection, current_index, 'mappings', index]
                                              for index, _ in matches]
                if getattr(mapping, 'kind', None) == 'metadata_column':
                    continue
                if kind == 'entity' and mapping.label_column and not any(
                        candidate.label_column == mapping.label_column for _, candidate in matches):
                    new_roles = []
                    if any(mapping.label_column in candidate.alias_columns for _, candidate in matches):
                        new_roles.append('alias')
                    if any(mapping.label_column in candidate.display_columns.values() for _, candidate in matches):
                        new_roles.append('display')
                    if any(mapping.label_column in candidate.properties.values() for _, candidate in matches):
                        new_roles.append('property')
                    add('revision_primary_name_role_changed',
                        '原主名称列不再作为相同物理映射的主名称；别名和展示字段不能替代主名称一致性检查。',
                        **location, column=mapping.label_column, previous_role='label',
                        current_roles=new_roles or ['unmapped'],
                        current_label_columns=sorted({candidate.label_column for _, candidate in matches
                                                      if candidate.label_column}))
                retained_context = {column for _, candidate in matches for column in candidate.context_columns}
                removed_context = sorted(set(mapping.context_columns) - retained_context)
                if removed_context:
                    add('revision_context_columns_removed', '原有路径或行上下文列已从相同物理映射中移除。',
                        **location, removed_columns=removed_context)
    return advice
