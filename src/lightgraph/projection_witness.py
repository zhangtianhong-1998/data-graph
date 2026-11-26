"""Evidence for a complete typed projection tuple, never a physical database row."""


def source_projection_witness(value):
    if not isinstance(value, dict) or value.get('kind') != 'source_projection_v1':
        return None
    if value.get('complete') is not True or value.get('basis') != 'complete_distinct_projection':
        return None
    for name in ('source_binding', 'projection', 'tuple'):
        token = value.get(name)
        if not isinstance(token, str) or len(token) != 64 or any(c not in '0123456789abcdef' for c in token):
            return None
    columns = value.get('columns')
    if not isinstance(columns, list) or not columns or any(not isinstance(c, str) or not c for c in columns):
        return None
    if len(set(columns)) != len(columns):
        return None
    return {key: value[key] for key in ('kind', 'basis', 'complete', 'source_binding', 'projection', 'tuple', 'columns')}
