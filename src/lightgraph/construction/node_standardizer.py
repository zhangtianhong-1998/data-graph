"""Standardized, metadata-enriched node detail view without exposing raw internal flags."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from typing import Any

from lightgraph.contracts import canonical
from .contexts import support_business_context, normalize_business_context
from .display import entity_name

_SOURCE_KIND_LABELS = {
    'gauss_database': 'GaussDB',
    'mysql_database': 'MySQL',
    'excel_workbook': 'Excel',
    'csv_file': 'CSV',
    '__base_graph__': '继承基准图谱',
}


def _catalog_index(catalog_tables: list[dict[str, Any]] | None) -> dict[tuple[str, str, str], dict[str, Any]]:
    index: dict[tuple[str, str, str], dict[str, Any]] = {}
    for table in catalog_tables or []:
        source_id = str(table.get('source_id') or table.get('source') or '')
        schema = str(table.get('schema') or table.get('schema_name') or 'public')
        name = str(table.get('name') or '')
        if not source_id or not name:
            continue
        columns = {str(col['name']): col for col in table.get('columns', []) if isinstance(col, dict) and col.get('name')}
        index[(source_id, schema, name)] = {
            'source_id': source_id,
            'schema': schema,
            'name': name,
            'comment': str(table.get('comment') or ''),
            'source_kind': table.get('source_kind') or '',
            'columns': columns,
        }
    return index


def _table_key_from_source(source: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(source.get('source_id') or ''),
        str(source.get('schema') or source.get('schema_name') or 'public'),
        str(source.get('table') or ''),
    )


def _qualified_table(source: dict[str, Any]) -> str:
    schema = source.get('schema') or source.get('schema_name') or 'public'
    table = source.get('table') or ''
    return f'{schema}.{table}' if table else ''


def standardize_node_details(
    node: dict[str, Any],
    *,
    definitions: dict[str, Any] | None = None,
    catalog_tables: list[dict[str, Any]] | None = None,
    property_labels: dict[str, str] | None = None,
    visible_values: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Build a clean, normalized node detail payload strictly from authorized supports."""
    catalog = _catalog_index(catalog_tables)
    supports = node.get('supports') or []
    entity_type = str(node.get('kind') or node.get('type') or '')
    entity_def = next(
        (item for item in (definitions or {}).get('entities', []) if item.get('type') == entity_type),
        {},
    )

    label_origin = str(node.get('label_origin') or '')
    source_labels = sorted({
        str(s['label']).strip()
        for s in supports
        if isinstance(s.get('label'), str) and s['label'].strip()
        and s.get('source_kind') not in {'catalog', 'declared'}
    })
    if not source_labels and label_origin == 'source' and isinstance(node.get('label'), str) and node['label'].strip():
        source_labels = [node['label'].strip()]
    display_name = entity_name(node)
    source_name = display_name if display_name in source_labels else next(iter(source_labels), None)

    names_by_priority = defaultdict(set)
    for support in supports:
        label, priority = support.get('label'), support.get('label_priority')
        if (isinstance(label, str) and label.strip() and isinstance(priority, int)
                and support.get('source_kind') not in {'catalog', 'declared'}):
            names_by_priority[priority].add(label.strip())
    preferred = names_by_priority[min(names_by_priority)] if names_by_priority else set()

    explicit_english = sorted({
        str(s['english_name']).strip()
        for s in supports
        if isinstance(s.get('english_name'), str) and s['english_name'].strip()
    })
    # Never guess English names from legacy aliases when english_name_column was not mapped.
    english_name = explicit_english[0] if explicit_english else None
    english_name_status = 'mapped' if english_name is not None else 'unknown'

    # Separate node business description from physical catalog table/column comments.
    row_descriptions = sorted({
        str(s['description']).strip()
        for s in supports
        if isinstance(s.get('description'), str) and s['description'].strip()
    })
    def_description = str(entity_def.get('description') or '').strip()
    if row_descriptions:
        business_description = '；'.join(row_descriptions)
        business_description_origin = 'source_column'
    elif def_description:
        business_description = def_description
        business_description_origin = 'entity_definition'
    else:
        business_description = None
        business_description_origin = 'none'

    # Collect attribute provenance and metadata column types dynamically from physical catalog.
    attr_origins: dict[str, dict[tuple[str, str, str, str], dict[str, Any]]] = defaultdict(dict)
    source_groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    catalog_notes_map: dict[tuple[str, str, str], dict[str, Any]] = {}

    observations = defaultdict(list)
    for support in supports:
        prov = support.get('provenance') if isinstance(support.get('provenance'), dict) else {}
        src = prov.get('source') if isinstance(prov.get('source'), dict) else {}
        tkey = _table_key_from_source(src)
        table_meta = catalog.get(tkey)
        qtable = _qualified_table(src)
        source_id = tkey[0] or 'unknown'

        if table_meta and (table_meta['comment'] or any(c.get('comment') for c in table_meta['columns'].values())):
            relevant_cols = set(prov.get('key_columns') or [])
            for name in (prov.get('label_column'), prov.get('english_name_column'), prov.get('description_column')):
                if name:
                    relevant_cols.add(name)
            relevant_cols.update(prov.get('columns') or [])
            col_comments = [
                {'column': col_name, 'data_type': col_info.get('data_type') or 'unknown',
                 'comment': str(col_info.get('comment') or '')}
                for col_name, col_info in sorted(table_meta['columns'].items())
                if col_name in relevant_cols and col_info.get('comment')
            ]
            if table_meta['comment'] or col_comments:
                note = catalog_notes_map.setdefault(tkey, {
                    'source_id': source_id,
                    'qualified_table': qtable,
                    'table_comment': table_meta['comment'],
                    'column_comments': {},
                    'note_kind': 'physical_catalog_declaration',
                })
                note['column_comments'].update({item['column']: item for item in col_comments})

        group = source_groups.setdefault((*tkey, canonical(support.get('inherited_from'))), {
            'source_id': source_id,
            'source_kind': table_meta['source_kind'] if table_meta and table_meta['source_kind'] else (
                prov.get('source_kind') or ''),
            'schema': tkey[1],
            'table': tkey[2],
            'qualified_table': qtable or source_id,
            'key_columns': [],
            'contributed_fields': [],
            '_seen_fields': set(),
            'scopes': set(),
            'evidence_refs': set(),
            'inherited_from': deepcopy(support.get('inherited_from')),
        })
        for column in prov.get('key_columns') or []:
            if column not in group['key_columns']:
                group['key_columns'].append(column)
        if support.get('scope_ref'):
            group['scopes'].add(str(support['scope_ref']))
        for ref in prov.get('evidence_refs') or []:
            group['evidence_refs'].add(str(ref))
        if support.get('evidence_refs'):
            for ref in support['evidence_refs']:
                group['evidence_refs'].add(str(ref))

        def add_field(role: str, label: str, column: str | None) -> None:
            if not column:
                return
            token = (role, label, column)
            if token in group['_seen_fields']:
                return
            group['_seen_fields'].add(token)
            col_meta = (table_meta or {}).get('columns', {}).get(column, {})
            group['contributed_fields'].append({
                'role': role,
                'field': label,
                'column': column,
                'data_type': col_meta.get('data_type') or 'unknown',
            })

        for kcol in prov.get('key_columns') or []:
            add_field('identity', '身份键', kcol)
        if support.get('label') is not None:
            add_field('chinese_name', '主名称', prov.get('label_column'))
        if support.get('english_name') is not None:
            add_field('english_name', '英文名', prov.get('english_name_column'))
        if support.get('description') is not None:
            add_field('description', '节点描述', prov.get('description_column'))
        for acol in prov.get('alias_columns') or []:
            if support.get('aliases'):
                add_field('alias', '别名', acol)
        if support.get('source_kind') == 'catalog' or prov.get('source_kind') == 'metadata_column':
            for col_name in prov.get('columns') or []:
                add_field('metadata_column', '物理列定义', col_name)

        prop_col_map = prov.get('property_columns') if isinstance(prov.get('property_columns'), dict) else {}
        # One table column can play a different role in another draft, even on
        # the same node. Classify each value against the mapping that read it.
        dedicated_columns = set(prov.get('key_columns') or []) | set(prov.get('alias_columns') or [])
        dedicated_columns.update(prov.get(role) for role in ('label_column', 'english_name_column', 'description_column'))
        entries = support.get('property_observations', [])
        if not entries:
            entries = [{'property': name, 'original_property': name, 'value': value, 'raw_value': value}
                       for name, values in (support.get('properties') or {}).items() for value in values]
        for entry in entries:
            attr_key = entry['property']
            if visible_values is not None and (attr_key, canonical(entry['value'])) not in visible_values:
                continue
            col_name = prop_col_map.get(entry['original_property'], '')
            col_meta = (table_meta or {}).get('columns', {}).get(col_name, {})
            dtype = col_meta.get('data_type') or 'unknown'
            if col_name:
                add_field('attribute', (property_labels or {}).get(attr_key, attr_key), col_name)
            origin = {'source_id': source_id, 'qualified_table': qtable or source_id,
                      'column': col_name or None, 'data_type': dtype}
            attr_origins[attr_key][(source_id, tkey[1], tkey[2], col_name)] = origin
            if col_name and src.get('source_id') and src.get('table'):
                binding_role = 'dedicated_column' if col_name in dedicated_columns else 'attribute_column'
            elif entry['original_property'] in (prov.get('constant_properties') or {}):
                binding_role = 'mapping_constant'
            else:
                binding_role = 'unlocated'
            value_origin = {**origin, 'binding_role': binding_role, 'value': entry['value'], 'raw_value': entry['raw_value'],
                            'original_property': entry['original_property'], 'scope_ref': support.get('scope_ref'),
                            'business_context': support_business_context(support),
                            'evidence_refs': sorted(set(prov.get('evidence_refs', [])) | set(support.get('evidence_refs', [])))}
            for detail in ('observation_id', 'row_context', 'execution_origins'):
                if detail in support:
                    value_origin[detail] = deepcopy(support[detail])
            observations[attr_key].append(value_origin)

    # Construction graph attributes live under properties. Presentation and
    # future runtime fields at the node root are never business attributes.
    raw_props = node.get('properties') or {}
    attributes = []
    for key in sorted(raw_props):
        values = raw_props[key]
        norm_values = list(values) if isinstance(values, list) else [values]
        if visible_values is not None:
            norm_values = [v for v in norm_values if (key, canonical(v)) in visible_values]
            if not norm_values:
                continue
        origins = list(attr_origins.get(key, {}).values())
        dtypes = sorted({o['data_type'] for o in origins if o.get('data_type') and o['data_type'] != 'unknown'})
        attributes.append({
            'key': key,
            'label': (property_labels or {}).get(key, key),
            'values': norm_values,
            'observations': sorted(observations.get(key, []), key=canonical),
            'data_type': dtypes[0] if len(dtypes) == 1 else (' / '.join(dtypes) if dtypes else 'unknown'),
            'origin_tables': [
                {
                    'source_id': o['source_id'],
                    'qualified_table': o['qualified_table'],
                    'column': o['column'],
                    'data_type': o['data_type'],
                }
                for o in origins
            ],
        })

    source_evidences = []
    for tkey in sorted(source_groups):
        item = source_groups[tkey]
        item.pop('_seen_fields', None)
        item['scopes'] = sorted(item['scopes'])
        item['evidence_refs'] = sorted(item['evidence_refs'])
        item['source_kind_label'] = _SOURCE_KIND_LABELS.get(item['source_kind'], item['source_kind'] or '数据源')
        source_evidences.append(item)

    for note in catalog_notes_map.values():
        note['column_comments'] = [note['column_comments'][key] for key in sorted(note['column_comments'])]

    return {
        'id': node.get('id'),
        'kind': entity_type,
        'type_family': node.get('original_type') or entity_def.get('original_type') or entity_type,
        'name': display_name,
        'status': node.get('status', 'accepted'),
        'source_name': source_name,
        'source_names': source_labels,
        'preferred_source_name': next(iter(preferred)) if len(preferred) == 1 else None,
        'chinese_name': source_name,  # Legacy API name for the mapped primary label.
        'english_name': english_name,
        'english_name_status': english_name_status,
        'display_name': display_name,
        'label_variants': list(node.get('label_variants', [])),
        'label_conflict': bool(node.get('label_conflict')),
        'aliases': list(node.get('aliases') or []),
        'identity': deepcopy(node.get('identity') or {}),
        'identity_types': deepcopy(node.get('identity_types') or {}),
        'identity_context': normalize_business_context(node.get('identity_context')),
        'business_contexts': sorted({canonical(support_business_context(s)): support_business_context(s)
                                     for s in supports}.values(), key=canonical) or [normalize_business_context(None)],
        'business_description': business_description,
        'business_description_origin': business_description_origin,
        'catalog_metadata_notes': [catalog_notes_map[k] for k in sorted(catalog_notes_map)],
        'attributes': attributes,
        'source_evidences': source_evidences,
        'scopes': list(node.get('scopes') or sorted({s for g in source_evidences for s in g['scopes']})),
    }
