"""Validate the physical row locator retained on worksheet observations."""


def worksheet_row_witness(value):
    """Read a complete physical worksheet locator without copying unrelated support fields."""
    if not isinstance(value, dict) or value.get('kind') != 'excel_rows_v1':
        return None
    snapshot, rows = value.get('snapshot'), value.get('rows')
    if (not isinstance(snapshot, str) or len(snapshot) != 64 or any(c not in '0123456789abcdef' for c in snapshot)
            or not isinstance(rows, list) or not rows):
        return None
    last = 0
    for span in rows:
        if (not isinstance(span, list) or len(span) != 2 or any(type(v) is not int for v in span)
                or not last < span[0] <= span[1] <= 1_048_576):
            return None
        last = span[1]
    return {'kind': 'excel_rows_v1', 'snapshot': snapshot, 'rows': rows}
