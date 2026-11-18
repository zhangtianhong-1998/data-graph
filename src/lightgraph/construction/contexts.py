"""Explicit business applicability; never inferred from provenance or access scope.

Each support is one observation and carries its own dimensions. Missing legacy
metadata means unknown, not globally applicable. Identity boundaries are a
separate declared contract whose dimensions must already be part of the key.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from lightgraph.contracts import canonical
from lightgraph.errors import LightGraphError
from lightgraph.source_values import plain_value, scalar_kind


def normalize_context_selection(selection: Any) -> dict[str, Any] | None:
    """Validate an optional conjunction of semantic dimension/value selectors."""
    if selection is None:
        return None
    if not isinstance(selection, dict) or len(selection) > 32:
        raise LightGraphError('invalid_business_context_selection')
    result = {}
    for name, value in selection.items():
        if not isinstance(name, str) or not name.strip() or value is None or not isinstance(value, (str, int, float, bool)):
            raise LightGraphError('invalid_business_context_selection')
        try:
            scalar_kind(value)
            canonical(value)
        except (LightGraphError, TypeError, ValueError):
            raise LightGraphError('invalid_business_context_selection') from None
        result[name] = plain_value(value)
    return dict(sorted(result.items()))


def normalize_business_context(context: Any) -> dict[str, Any]:
    """Read old and new artifacts conservatively; malformed scopes stay unknown."""
    result = {'version': 1, 'status': 'unknown', 'dimensions': {}, 'value_types': {}}
    if not isinstance(context, dict) or context.get('version', 1) != 1:
        return result
    dimensions = context.get('dimensions')
    if not isinstance(dimensions, dict):
        return result
    try:
        clean = normalize_context_selection(dimensions) if dimensions else {}
    except LightGraphError:
        return result
    result['dimensions'] = clean
    types = context.get('value_types') if isinstance(context.get('value_types'), dict) else {}
    result['value_types'] = {name: types.get(name) or scalar_kind(value) for name, value in clean.items()}
    status = context.get('status')
    if not (context.get('missing_dimensions') or context.get('conflicts')) and (
            (status == 'global' and not clean) or (status == 'scoped' and clean)):
        result['status'] = status
    for name in ('declared_status', 'missing_dimensions', 'observed_columns', 'declared_dimensions',
                 'origin', 'conflicts', 'identity_context'):
        if name in context:
            result[name] = deepcopy(context[name])
    return result


def support_business_context(support: dict[str, Any]) -> dict[str, Any]:
    return normalize_business_context(support.get('business_context'))


def business_context_matches(support: dict[str, Any], selection: dict[str, Any] | None) -> bool:
    """No selection preserves old facts; a selection excludes unknown applicability."""
    selection = normalize_context_selection(selection)
    if selection is None:
        return True
    context = support_business_context(support)
    if context['status'] == 'global':
        return True
    if not selection:
        return False
    if context['status'] != 'scoped':
        return False
    return all(name in context['dimensions'] and canonical(context['dimensions'][name]) == canonical(value)
               for name, value in selection.items())


def business_context_identity(support: dict[str, Any]) -> dict[str, Any]:
    """Stable applicability identity, excluding evidence and execution metadata."""
    context = support_business_context(support)
    return {key: context[key] for key in ('status', 'dimensions', 'value_types')}


def intersect_business_contexts(*contexts: dict[str, Any]) -> dict[str, Any] | None:
    """Conjoin actual witnesses without promoting an unknown observation to global."""
    if not contexts:
        return normalize_business_context(None)
    dimensions, types = {}, {}
    unknown = False
    for raw in contexts:
        context = normalize_business_context(raw)
        unknown = unknown or context['status'] == 'unknown'
        for name, value in context['dimensions'].items():
            value_type = context['value_types'][name]
            if name in dimensions and canonical([types[name], dimensions[name]]) != canonical([value_type, value]):
                return None
            dimensions[name], types[name] = value, value_type
    return {'version': 1, 'status': 'unknown' if unknown else 'scoped' if dimensions else 'global',
            'dimensions': dict(sorted(dimensions.items())), 'value_types': dict(sorted(types.items())),
            'origin': 'witness_context_intersection'}


def identity_business_context(definition, identity: dict[str, Any], identity_types: dict[str, str]) -> dict[str, Any]:
    names = definition.identity_context_keys
    return {'version': 1, 'status': definition.identity_scope,
            'dimensions': {name: plain_value(identity[name]) for name in names},
            'value_types': {name: identity_types[name] for name in names}}


def materialize_business_context(mapping, row: dict[str, Any], *, identity_context=None) -> dict[str, Any]:
    """Bind declared semantic roles to one row; null dimensions never become global."""
    spec = mapping.business_context
    dimensions = dict(spec.values)
    dimensions.update({name: row[column] for name, column in spec.columns.items()})
    missing = sorted(name for name, value in dimensions.items() if value is None or value == '')
    dimensions = {name: value for name, value in dimensions.items() if name not in missing}
    status = spec.status
    conflicts = []
    if identity_context and identity_context['status'] == 'scoped':
        if status == 'global':
            conflicts.append({'reason': 'scoped_identity_has_global_fact_context'})
        for name, value in identity_context['dimensions'].items():
            if name in dimensions and canonical(plain_value(dimensions[name])) != canonical(value):
                conflicts.append({'reason': 'business_context_identity_conflict', 'dimension': name,
                                  'identity_value': value, 'fact_value': plain_value(dimensions[name])})
            else:
                dimensions[name] = value
        status = 'scoped'
    result = {'version': 1, 'status': 'unknown' if missing or conflicts else status,
              'dimensions': plain_value(dict(sorted(dimensions.items()))),
              'value_types': {name: scalar_kind(value) for name, value in sorted(dimensions.items())},
              'origin': 'declared_mapping',
              'observed_columns': dict(spec.columns), 'declared_dimensions': sorted(spec.values)}
    if identity_context:
        for name in identity_context['dimensions']:
            if name not in {conflict.get('dimension') for conflict in conflicts}:
                result['value_types'][name] = identity_context['value_types'][name]
    if missing:
        result.update(declared_status=status, missing_dimensions=missing)
    if conflicts:
        # Keep the original observation and both statements for review. Neither
        # side wins and a contradictory applicability claim is never global.
        result.update(declared_status=spec.status, conflicts=conflicts, identity_context=deepcopy(identity_context))
    return result
