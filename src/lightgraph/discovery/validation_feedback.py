"""Bounded validation feedback made only from schema vocabulary and positions.

Validation exceptions contain source values in messages, context and map keys.
Never serialize them. These helpers retain useful field/constraint information
without returning input values or arbitrary property names to the Agent.
"""
from __future__ import annotations

from itertools import islice

from jsonschema import Draft202012Validator
from pydantic import ValidationError


MAX_DIAGNOSTICS = 8
_JSON_CONSTRAINTS = frozenset({
    'type', 'enum', 'const', 'required', 'additionalProperties', 'minLength',
    'maxLength', 'minItems', 'maxItems', 'uniqueItems', 'minimum', 'maximum',
    'exclusiveMinimum', 'exclusiveMaximum', 'pattern', 'anyOf', 'oneOf',
    'allOf', 'not', 'minProperties', 'maxProperties',
})
_PYDANTIC_CONSTRAINTS = {
    'missing': 'required', 'extra_forbidden': 'additionalProperties',
    'literal_error': 'enum', 'string_too_short': 'minLength',
    'string_too_long': 'maxLength', 'too_short': 'minItems',
    'too_long': 'maxItems', 'greater_than': 'exclusiveMinimum',
    'greater_than_equal': 'minimum', 'less_than': 'exclusiveMaximum',
    'less_than_equal': 'maximum', 'value_error': 'semantic_contract',
}
# Validator messages here are fixed codes, never source text. Only these may
# replace the generic semantic_contract label shown to the agent.
_SAFE_VALUE_CODES = frozenset({
    'object_model_identity_fields_required', 'object_model_code_label_overlap',
    'object_model_duplicate_position', 'duplicate_hierarchy_position',
    'duplicate_key_column', 'excluded_role_in_identity', 'abstain_with_proposals',
    'task_kind_forbidden', 'identity_domain_policy_conflict',
    'global_identity_scope_evidence_required', 'verified_identity_domain_evidence_required',
    'hierarchy_plan_duplicate_position', 'hierarchy_plan_shared_domain_required',
})


def _property_names(schema):
    names = set()
    def visit(value):
        if isinstance(value, dict):
            names.update(value.get('properties', {}))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(schema)
    return names


def _path(parts, names):
    # Unknown dictionary keys may themselves contain source values. Array
    # positions are structural; only names declared by the schema may escape.
    return [part if type(part) is int and 0 <= part < 1000000 else
            part if isinstance(part, str) and part in names else '*'
            for part in list(parts)[:16]]


def _static_values(values):
    if not isinstance(values, list) or len(values) > 32:
        return None
    if all(type(v) in (str, int, float, bool, type(None)) and
           (not isinstance(v, str) or len(v) <= 128) for v in values):
        return values
    return None


def schema_diagnostics(validator: Draft202012Validator, arguments) -> list[dict]:
    """Return schema paths and constraints, never jsonschema's error messages."""
    names = _property_names(validator.schema)
    result = []
    for error in islice(validator.iter_errors(arguments), MAX_DIAGNOSTICS):
        constraint = error.validator if error.validator in _JSON_CONSTRAINTS else 'schema'
        path = _path(error.absolute_path, names)
        # Missing field names come from the static required list, not message
        # parsing. For extra fields, report the parent without echoing its keys.
        if constraint == 'required' and isinstance(error.instance, dict):
            missing = [name for name in error.validator_value
                       if name in names and name not in error.instance]
            for name in missing[:MAX_DIAGNOSTICS - len(result)]:
                entry = {'path': [*path, name], 'constraint': 'required'}
                if entry not in result:
                    result.append(entry)
        else:
            entry = {'path': path, 'constraint': constraint}
            if constraint in ('enum', 'const'):
                allowed = _static_values(error.validator_value if constraint == 'enum'
                                         else [error.validator_value])
                if allowed is not None:
                    entry['allowed_values'] = allowed
            if entry not in result:
                result.append(entry)
        if len(result) >= MAX_DIAGNOSTICS:
            break
    return result


def model_diagnostics(error: ValidationError, schema: dict, arguments) -> list[dict]:
    """Use static schema checks first, then sanitize model-only constraints."""
    result = schema_diagnostics(Draft202012Validator(schema), arguments)
    if result:
        return result
    names = _property_names(schema)
    diagnostics=[]
    for item in error.errors(include_url=False, include_context=True,
                             include_input=False)[:MAX_DIAGNOSTICS]:
        constraint=_PYDANTIC_CONSTRAINTS.get(item['type'], 'type')
        if item.get('type')=='value_error':
            cause=(item.get('ctx') or {}).get('error')
            arguments=getattr(cause,'args',())
            if len(arguments)==1 and isinstance(arguments[0], str) and arguments[0] in _SAFE_VALUE_CODES:
                constraint=arguments[0]
        diagnostics.append({'path': _path(item['loc'], names), 'constraint': constraint})
    return diagnostics
