"""Corroborate a saved mapped property with an independent cell on the same worksheet rows."""
from collections import Counter, defaultdict

from lightgraph.contracts import canonical
from lightgraph.source_values import scalar_kind


def _mapped_values(support):
    prov = support['provenance']
    mapping = prov.get('property_columns') or {}
    if not mapping:
        return {}
    local = support.get('properties') or {}
    constants = prov.get('constant_properties') or {}
    row_columns = set(prov.get('context_columns') or ())
    projected = set(prov.get('columns') or ())
    occurrences = Counter(mapping.values())
    result = {}
    for name, column in mapping.items():
        values = local.get(name)
        if (column not in projected or column in row_columns or name in constants or occurrences[column] != 1
                or not isinstance(values, list) or len(values) != 1):
            continue
        value = values[0]
        if isinstance(value, (str, int, float, bool)) and value != '':
            # Compare native types too: a stored string cannot certify a date
            # that happens to serialize to the same characters.
            result[column] = (name, canonical([scalar_kind(value), value]))
    return result


def _typed_witnesses(support, columns):
    """Use directly recorded row cells and check any local attributes bound to them."""
    prov = support['provenance']
    row = support.get('row_context') or {}
    values = row.get('values') or {}
    selected = values.keys() & columns
    inconsistent = set()
    mapped = prov.get('property_columns') or {}
    observed = support.get('properties') or {}
    constants = prov.get('constant_properties') or {}
    for name, col in mapped.items():
        if col in selected and (name in constants or canonical(observed.get(name)) != canonical([values[col]])):
            inconsistent.add(col)
    cells = [(col, prov['source_column_ordinals'][col], canonical([row['value_types'][col], values[col]]))
             for col in selected - inconsistent]
    return cells, inconsistent


def worksheet_property_matches(entries, sections):
    """Return comparison-only overrides keyed by support index and original row section.

    The caller already grouped one physical endpoint pair on the same sheet.
    Only sections that survived full header, shared-cell and row-budget checks
    can provide evidence. Index by interval; never expand to individual rows.
    """
    candidates = {}
    located = []
    for index, ((_, support, _), boundaries) in enumerate(zip(entries, sections, strict=True)):
        if not boundaries or not isinstance(boundaries[0].row_context, tuple):
            continue
        unit = support.get('contributor_id')
        if not unit or any(o.get('contributor_id', unit) != unit for o in support.get('execution_origins', ())):
            continue
        located.append((index, unit, support, boundaries))
        if values := _mapped_values(support):
            candidates[index] = values
    if not candidates:
        return {}

    columns = {col for values in candidates.values() for col in values}
    names = {name for values in candidates.values() for name, _ in values.values()}
    witnesses = defaultdict(set)
    property_sources = defaultdict(set)
    inconsistent_cells = set()
    for _, unit, support, boundaries in located:
        prov = support['provenance']
        mapped = prov.get('property_columns') or {}
        sources = [(name, mapped[name]) for name in mapped.keys() & names]
        cells, inconsistent = _typed_witnesses(support, columns)
        for boundary in boundaries:
            location = (boundary.scope, boundary.predicate, boundary.row_context[:4])
            inconsistent_cells.update((location, col) for col in inconsistent)
            for name, column in sources:
                property_sources[location, name].add(column)
            for cell in cells:
                witnesses[location, cell].add(unit)

    matches = {}
    for index, unit, support, boundaries in located:
        if index not in candidates:
            continue
        ordinals = support['provenance']['source_column_ordinals']
        for boundary in boundaries:
            section = boundary.row_context[:4]
            location = (boundary.scope, boundary.predicate, section)
            binding = boundary.row_context[4]
            cells = {b[0]: b[1] for b in binding if len(b) == 2}
            corroborated = set()
            for col, (name, typed_value) in candidates[index].items():
                units = witnesses.get((location, (col, ordinals[col], typed_value)))
                if (units and (len(units) > 1 or unit not in units) and len(property_sources[location, name]) == 1
                        and (location, col) not in inconsistent_cells and cells.get(col, typed_value) == typed_value):
                    cells[col] = typed_value
                    corroborated.add(name)
            if corroborated:
                dimensions = tuple(b for b in binding if len(b) != 2)
                aligned = boundary._replace(row_context=(*section, tuple(sorted(cells.items())) + dimensions))
                matches.setdefault(index, {})[boundary] = (aligned, frozenset(corroborated))
    return matches
