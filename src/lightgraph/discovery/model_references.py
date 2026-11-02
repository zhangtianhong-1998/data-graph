"""Ephemeral short references at the model boundary, with canonical IDs inside.

Only issued identifiers in typed reference fields are translated. SQL, source
values and prose never undergo string replacement. A short spelling grants no
authority: the ordinary schema, scope and evidence checks still run afterwards.
"""
from __future__ import annotations

from copy import deepcopy
import json

from lightgraph.contracts import canonical
from lightgraph.errors import LightGraphError


_FIELDS = frozenset({
    'id', 'column_id', 'object_id', 'rule_id', 'candidate_id', 'draft_id',
    'replace_draft_id', 'revision_of', 'superseded_by', 'proposition_ref',
    'source_rule_id', 'target_rule_id',
    'semantic_proposition_ref', 'reference', 'issued_reference', 'invalid_reference',
    'evidence_ref', 'definition_ref', 'scope_evidence_ref', 'condition_ref',
    'selector_ref', 'applies_to_entity', 'source_entity', 'target_entity',
    'column', 'key_column', 'parent_column', 'label_column', 'source_value_column',
    'polymorphic_by', 'discriminating_column',
    'columns', 'key_columns', 'namespace_columns', 'label_columns',
    'english_name_columns', 'alias_columns', 'source_columns', 'target_columns',
    'source_json_array_columns', 'target_json_array_columns',
    'aliases', 'single_valued', 'multi_valued', 'display_only', 'not_excluded',
    'unassigned_columns', 'authorized_entities', 'authorized_selectors',
    'supersedes_object_ids', 'retired_object_ids', 'rule_refs', 'evidence_cited',
    'evidence_refs', 'identity_evidence', 'attribute_evidence', 'global_scope_evidence',
    'ordering_evidence', 'taxonomy_evidence', 'position_evidence', 'context_evidence',
    'dag_evidence', 'blocking_evidence_refs', 'issued_evidence_refs',
    'required_document_refs', 'required_evidence_refs', 'allowed_references', 'required_references',
    'allowed_evidence_refs', 'invalid_evidence_refs', 'entity_kind_definition_refs', 'semantic_basis',
    'definition_sections', 'evidence', 'entities', 'selectors', 'probe_refs',
})
_OPAQUE_FIELDS = {'object_id': 'obj', 'rule_id': 'rule', 'candidate_id': 'candidate',
                  'draft_id': 'draft', 'semantic_proposition_ref': 'claim',
                  'source_rule_id': 'rule', 'target_rule_id': 'rule', 'condition_ref': 'condition'}
_LITERAL = frozenset({'text', 'description', 'explanation', 'rationale', 'subtask',
    'sql', 'parameters', 'goal', 'runtime_guidance', 'condition', 'name',
    'entity_kind', 'entity_kind_hint', 'relation', 'property_name', 'properties'})


class ModelReferences:
    """One task's append-only alias registry; never serialized into artifacts."""

    def __init__(self, packet=None):
        self.forward = {}
        self.reverse = {}
        self._counts = {}
        self._reserved = set()
        if packet is not None:
            self.observe(packet)

    def _issue(self, value, prefix):
        if not isinstance(value, str):
            return
        if value in self.reverse:
            raise LightGraphError('model_reference_collision')
        if len(value) < 16 or value in self.forward:
            return
        number = self._counts.get(prefix, 0) + 1
        alias = f'{prefix}{number}'
        while alias in self._reserved or alias in self.reverse:
            number += 1
            alias = f'{prefix}{number}'
        self._counts[prefix] = number
        self.forward[value] = alias
        self.reverse[alias] = value

    @staticmethod
    def _keys(value, mapping):
        result = {}
        for key, item in value.items():
            translated = mapping.get(key, key)
            if translated in result:
                raise LightGraphError('model_reference_collision')
            result[translated] = deepcopy(item)
        return result

    def observe(self, value):
        """Register runtime metadata, never identifiers guessed from row values."""
        identifiers = []
        self._collect(value, identifiers)
        # Reserve even short canonical IDs before assigning any aliases. A real
        # object named obj1 must never be confused with another object's alias.
        for identifier, _prefix in identifiers:
            if identifier in self.reverse:
                raise LightGraphError('model_reference_collision')
        self._reserved.update(identifier for identifier, _prefix in identifiers)
        for identifier, prefix in identifiers:
            self._issue(identifier, prefix)

    def _collect(self, value, identifiers):
        def add(identifier, prefix):
            if isinstance(identifier, str):
                identifiers.append((identifier, prefix))
        if isinstance(value, list):
            for item in value:
                self._collect(item, identifiers)
        elif isinstance(value, dict):
            if all(isinstance(value.get(k), str) for k in ('doc_id', 'revision', 'section_id')):
                add(f"{value['doc_id']}@{value['revision']}:{value['section_id']}", 'section')
                add(f"{value['doc_id']}@{value['revision']}", 'document')
            identifier = value.get('id')
            if isinstance(identifier, str) and 'name' in value:
                add(identifier, 'col')
            for field, prefix in _OPAQUE_FIELDS.items():
                add(value.get(field), prefix)
            for field, prefix in (('authorized_entities', 'entity'), ('authorized_selectors', 'selector')):
                for identifier in value.get(field, []) or []:
                    add(identifier, prefix)
            for field, item in value.items():
                if field not in _LITERAL | {'rows', 'samples', 'strata_samples', 'tool_observations'}:
                    self._collect(item, identifiers)

    def _translate(self, value, mapping, field='', *, encode=False):
        if isinstance(value, str):
            return mapping.get(value, value) if field in _FIELDS else value
        if isinstance(value, list):
            return [self._translate(item, mapping, field, encode=encode) for item in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            # Row *keys* may be column IDs. Cell values, including strings that
            # happen to equal an alias or ID, must remain byte-for-byte intact.
            if key == 'rows':
                if isinstance(item, list):
                    result[key] = [self._keys(row, mapping)
                                   if isinstance(row, dict) else deepcopy(row) for row in item]
                else:
                    result[key] = deepcopy(item)
            elif field in {'positions', 'levels'} and key in {'key', 'code', 'label', 'alias', 'display_order'}:
                result[key] = mapping.get(item, item) if isinstance(item, str) else deepcopy(item)
            elif key in _LITERAL:
                result[key] = deepcopy(item)
            elif key == 'property_names' and isinstance(item, dict):
                result[key] = self._keys(item, mapping)
            elif key in {'entity_bindings', 'aggregates'} and isinstance(item, dict):
                result[key] = self._translate(self._keys(item, mapping), mapping, key, encode=encode)
            elif key == 'aggregate_columns' and isinstance(item, dict):
                result[key] = {name: mapping.get(ref, ref) if isinstance(ref, str) else deepcopy(ref)
                               for name, ref in item.items()}
            else:
                result[key] = self._translate(item, mapping, key, encode=encode)
        if encode and all(isinstance(value.get(k), str) for k in ('doc_id', 'revision', 'section_id')):
            reference = f"{value['doc_id']}@{value['revision']}:{value['section_id']}"
            result['reference'] = mapping.get(reference, reference)
            version = f"{value['doc_id']}@{value['revision']}"
            result['document_version_reference'] = mapping.get(version, version)
        return result

    def response(self, message, submission_names=()):
        """Decode before Pydantic/Toolkit checks, preserving native call IDs."""
        from .llm import _strict_json_loads
        result = deepcopy(message)
        if not isinstance(result, dict):
            return result
        if result.get('tool_calls'):
            for call in result['tool_calls']:
                function = call.get('function', {}) if isinstance(call, dict) else {}
                if function.get('name') not in submission_names:
                    continue
                arguments = function.get('arguments')
                try:
                    value = _strict_json_loads(arguments) if isinstance(arguments, str) else arguments
                except (ValueError, TypeError):
                    continue  # Existing invalid-arguments feedback owns this case.
                function['arguments'] = canonical(self.decode(value))
        elif isinstance(result.get('content'), str):
            try:
                value = _strict_json_loads(result['content'])
            except (ValueError, TypeError):
                return result
            result['content'] = canonical(self.decode(value))
        return result

    def encode(self, value):
        return self._translate(value, self.forward, encode=True)

    def decode(self, value):
        return self._translate(value, self.reverse)

    def schema(self, value, field=''):
        """Translate enum/const only in typed reference slots, never vocabulary."""
        if isinstance(value, list):
            return [self.schema(item, field) for item in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, child in value.items():
            if key == 'properties' and isinstance(child, dict):
                result[key] = {name: deepcopy(spec) if name in _LITERAL else self.schema(spec, name)
                               for name, spec in child.items()}
            elif key == 'enum' and field in _FIELDS:
                result[key] = [self.forward.get(item, item) if isinstance(item, str) else item for item in child]
            elif key == 'const' and field in _FIELDS and isinstance(child, str):
                result[key] = self.forward.get(child, child)
            else:
                result[key] = self.schema(child, field if key in {'items', 'anyOf', 'oneOf', 'allOf'} else '')
        return result

    def messages(self, messages, submission_names=()):
        """Encode structured task/results and proposal history, not source SQL."""
        result = []
        for message in messages:
            item = deepcopy(message)
            if item.get('role') in {'user', 'tool'} and isinstance(item.get('content'), str):
                try:
                    payload = json.loads(item['content'])
                except (ValueError, TypeError):
                    pass
                else:
                    self.observe(payload)
                    encoded = self.encode(payload)
                    if isinstance(encoded, dict) and 'input' in encoded and self.forward:
                        encoded['reference_notation'] = (
                            'Short references (col1, section1, obj1, etc.) identify issued metadata in this task. '
                            'Copy them in reference fields. For documents copy reference or document_version_reference; '
                            'do not concatenate their components. They are not SQL identifiers or business values. '
                            'Use physical column names and quoted SQL identifiers for source tools. '
                            'The server resolves references before authorization and evidence validation.')
                    if isinstance(encoded, dict) and 'output_schema' in encoded:
                        encoded['output_schema'] = self.schema(payload['output_schema'])
                    item['content'] = canonical(encoded)
            for call in item.get('tool_calls', []):
                function = call.get('function', {})
                if function.get('name') in submission_names:
                    try:
                        args = json.loads(function['arguments'])
                    except (ValueError, TypeError, KeyError):
                        continue
                    function['arguments'] = canonical(self.encode(args))
            result.append(item)
        return result

    def clear(self):
        self.forward.clear()
        self.reverse.clear()
        self._counts.clear()
        self._reserved.clear()
