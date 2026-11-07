"""Preserve driver scalar types until identity/matching has been computed.

Typed text has the same JSON/display representation as before. Its type is
runtime evidence from the driver, never guessed from a catalog declaration or
the spelling of a CSV field. Persisted identities carry the type separately.
"""
from datetime import date, datetime, time
from decimal import Decimal
import math

from .contracts import canonical
from .errors import LightGraphError


SOURCE_KEY_SEMANTICS_VERSION = 'driver_scalar_v2'


class SourceText(str):
    __slots__ = ()


class DecimalText(SourceText):
    __slots__ = ()


class DateText(SourceText):
    __slots__ = ()


class DateTimeText(SourceText):
    __slots__ = ()


class TimeText(SourceText):
    __slots__ = ()


_KINDS = {DecimalText: 'decimal', DateText: 'date', DateTimeText: 'datetime', TimeText: 'time'}


def source_scalar(value):
    """Convert a driver value without confusing typed values with source text."""
    if isinstance(value, SourceText):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise LightGraphError('non_finite_source_value')
        return DecimalText(str(value))
    if isinstance(value, datetime):
        return DateTimeText(value.isoformat())
    if isinstance(value, date):
        return DateText(value.isoformat())
    if isinstance(value, time):
        return TimeText(value.isoformat())
    if isinstance(value, float) and not math.isfinite(value):
        raise LightGraphError('non_finite_source_value')
    if not isinstance(value, (str, int, float, bool, type(None))):
        raise LightGraphError('source_value_type_unsupported')
    return value


def scalar_kind(value):
    value = source_scalar(value)
    if type(value) in _KINDS:
        return _KINDS[type(value)]
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'boolean'
    if isinstance(value, int):
        return 'integer'
    if isinstance(value, float):
        return 'real'
    return 'text'


def _decimal_key(value):
    # Decimal.normalize() obeys the current decimal context and can round long
    # coefficients. Strip insignificant zeros using the exact tuple instead.
    sign, digits, exponent = Decimal(value).as_tuple()
    if not any(digits):
        return '0e0'
    digits = list(digits)
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    return ('-' if sign else '') + ''.join(map(str, digits)) + 'e' + str(exponent)


def scalar_cell(value):
    value = source_scalar(value)
    kind = scalar_kind(value)
    return kind, _decimal_key(value) if kind == 'decimal' else value


def typed_key(values):
    """One key contract for reference probes, endpoint lookup and key equality."""
    return canonical([scalar_cell(value) for value in values])


def identity_values(values):
    """Keep historical primitive IDs; distinguish newly preserved driver types."""
    return [typed_value(source_scalar(value)) for value in values]


def typed_value(value):
    """Type-preserving digest input, including nested source row contexts."""
    if isinstance(value, SourceText):
        kind, token = scalar_cell(value)
        return {'source_scalar_type': kind, 'value': token}
    if isinstance(value, dict):
        return {key: typed_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [typed_value(item) for item in value]
    return value


def plain_value(value):
    """Only remove runtime tags after matching; identity_types remain on disk."""
    if isinstance(value, SourceText):
        return str(value)
    if isinstance(value, dict):
        return {key: plain_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain_value(item) for item in value]
    return value


def persisted_identity_key(node):
    """Compare stored identities without treating display spelling as type.

    Decimal keys with different insignificant zeros have the same identity.
    Historical primitive identities without type metadata retain their types.
    """
    return canonical({name: identity_cell(node, name) for name in node.get('identity', {})})


def identity_cell(node, name):
    classes = {kind: cls for cls, kind in _KINDS.items()}
    kind = node.get('identity_types', {}).get(name)
    value = node['identity'][name]
    return scalar_cell(classes[kind](value) if kind in classes else value)
