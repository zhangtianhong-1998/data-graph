"""Issued-reference validation and value-free, in-memory protocol repair."""
from __future__ import annotations

from dataclasses import dataclass
from pydantic import Field, ValidationError

from lightgraph.contracts import StrictModel
from lightgraph.errors import LightGraphError
from .models import ProposalBatch


def reference_registry(packet):
    """Use the same revision-qualified section IDs as the semantic compiler."""
    documents = packet.get('document_snippets', []) + packet.get('document_refs', [])
    document_ids = {d['doc_id'] for d in documents if d.get('doc_id')}
    sections = {f"{d['doc_id']}@{d['revision']}:{d['section_id']}" for d in documents
                if all(d.get(k) for k in ('doc_id', 'revision', 'section_id'))}
    columns = {c['id'] for c in packet.get('metadata_digest', {}).get('columns', [])}
    probes = {p['probe_id'] for p in packet.get('probe_digest', [])}
    strata = {s['stratum'] for s in packet.get('strata_samples', [])}
    return {'columns': columns, 'evidence': columns | probes | strata | document_ids | sections,
            'entities': set(packet.get('authorized_entities', [])),
            'selectors': set(packet.get('authorized_selectors', []))}


@dataclass
class ReferenceSlot:
    proposal_index: int
    path: tuple[str | int, ...]
    category: str
    code: str
    allowed: set[str]
    map_key: bool = False

    def locate(self, raw):
        value = raw['proposals'][self.proposal_index]
        for part in self.path[:-1]:
            value = value[part]
        return value, self.path[-1]

    def get(self, raw):
        parent, key = self.locate(raw)
        return list(parent)[key] if self.map_key else parent[key]

    def put(self, raw, value):
        parent, key = self.locate(raw)
        if self.map_key:
            entries = list(parent.items())
            if value in parent and list(parent)[key] != value:
                raise LightGraphError('reference_repair_key_collision')
            entries[key] = (value, entries[key][1])
            parent.clear(); parent.update(entries)
        else:
            parent[key] = value

    def diagnostic(self, slot_id):
        # Paths contain schema names and offsets only, never an unissued key.
        return {'slot_id': slot_id, 'proposal_index': self.proposal_index,
                'field': list(self.path), 'map_key': self.map_key,
                'category': self.category, 'code': self.code,
                'allowed_references': sorted(self.allowed)}


def invalid_reference_slots(batch, packet):
    if batch.task != packet.get('task'):
        raise LightGraphError('model_task_mismatch')
    registry = reference_registry(packet)
    raw = batch.model_dump(mode='json')
    slots = []
    for index, proposal in enumerate(raw['proposals']):
        claim = proposal['claim']

        def add(path, category='columns', code='unauthorized_column_reference', *, allowed=None, map_key=False):
            slot = ReferenceSlot(index, tuple(path), category, code,
                                 registry[category] if allowed is None else allowed, map_key)
            value = slot.get(raw)
            if value is not None and value not in slot.allowed:
                slots.append(slot)

        def many(path, category='columns', code='unauthorized_column_reference'):
            values = proposal
            for part in path:
                values = values[part]
            for offset in range(len(values)):
                add([*path, offset], category, code)

        many(['evidence_cited'], 'evidence', 'dangling_evidence_reference')
        kind = proposal['kind']
        if kind == 'column_group':
            for member in range(len(claim['members'])):
                add(['claim', 'members', member, 'column_id'])
        elif kind == 'entity_key':
            for field in ('key_columns', 'namespace_columns'):
                many(['claim', field])
            for field in ('single_valued', 'multi_valued', 'display_only'):
                many(['claim', 'attributes', field])
            for offset in range(len(claim['attributes']['property_names'])):
                # Authorization is batch-wide. Binding a known column to an
                # attribute belongs to per-proposal compilation and repair.
                add(['claim', 'attributes', 'property_names', offset], map_key=True)
        elif kind == 'wide_hierarchy':
            many(['claim', 'namespace_columns'])
            for level in range(len(claim['levels'])):
                for field in ('key', 'label', 'alias', 'display_order'):
                    add(['claim', 'levels', level, field])
            for field in ('ordering_evidence', 'identity_evidence', 'attribute_evidence',
                          'global_scope_evidence', 'taxonomy_evidence', 'position_evidence',
                          'context_evidence', 'dag_evidence'):
                many(['claim', field], 'evidence',
                     'dangling_order_evidence' if field == 'ordering_evidence' else 'dangling_evidence_reference')
        elif kind == 'long_hierarchy':
            for field in ('key_column', 'parent_column', 'label_column'):
                add(['claim', field])
            for field in ('alias_columns', 'namespace_columns'):
                many(['claim', field])
            many(['claim', 'ordering_evidence'], 'evidence', 'dangling_order_evidence')
        elif kind == 'reference':
            for field in ('source_columns', 'target_columns'):
                many(['claim', field])
            add(['claim', 'polymorphic_by'])
            for field in ('source_entity', 'target_entity'):
                add(['claim', field], 'entities', 'unverified_entity_reference')
            add(['claim', 'condition_ref'], 'selectors', 'unauthorized_selector_reference')
        elif kind == 'discriminator':
            add(['claim', 'discriminating_column'])
            for part in range(len(claim['partitions'])):
                add(['claim', 'partitions', part, 'source_value_column'])
                add(['claim', 'partitions', part, 'selector_ref'], 'selectors', 'unauthorized_selector_reference')
                add(['claim', 'partitions', part, 'applies_to_entity'], 'entities', 'unverified_entity_reference')
        elif kind == 'exclusion':
            for item in range(len(claim['exclusions'])):
                add(['claim', 'exclusions', item, 'column_id'])
            many(['claim', 'not_excluded'])
    return slots


def validate_proposal_references(batch, packet):
    slots = invalid_reference_slots(batch, packet)
    if slots:
        raise LightGraphError(slots[0].code)


def normalize_column_references(batch, packet):
    """Resolve exact, unique authorized names only in typed column slots.

    Names are aliases for already issued metadata IDs, never new authority.
    Ambiguous catalog names, evidence references and free text stay untouched.
    A collision or invalid resulting claim leaves the batch for strict repair.
    """
    by_name = {}
    locations = {}
    scope = packet.get('scope')
    for column in packet.get('metadata_digest', {}).get('columns', []):
        by_name.setdefault(column['name'], set()).add(column['id'])
        if scope:
            locations.setdefault(column['name'], set()).add((scope['source_id'],
                scope.get('schema', 'public'), scope['table']))
    for table in packet.get('catalog', []):
        location = (table['source_id'], table.get('schema', 'public'), table['name'])
        for column in table.get('columns', []):
            locations.setdefault(column['name'], set()).add(location)
    aliases = {name: next(iter(ids)) for name, ids in by_name.items()
               if len(ids) == 1 and len(locations.get(name, ())) <= 1}
    raw = batch.model_dump(mode='json')
    changed = 0
    try:
        for slot in invalid_reference_slots(batch, packet):
            if slot.category != 'columns':
                continue
            replacement = aliases.get(slot.get(raw))
            if replacement is not None:
                slot.put(raw, replacement)
                changed += 1
        if changed:
            return ProposalBatch.model_validate(raw), changed
    except (LightGraphError, ValidationError):
        # Never overwrite one mapping with another or hide claim conflicts.
        pass
    return batch, 0


class ReferenceReplacement(StrictModel):
    slot_id: int = Field(ge=0)
    reference: str = Field(min_length=1, max_length=2048)


class ReferenceRepair(StrictModel):
    replacements: list[ReferenceReplacement] = Field(default_factory=list, max_length=128)
    abstain: bool = False


def validate_or_repair(client, batch, packet, *, max_repairs=2):
    """Patch only invalid references; no proposal or exclusion can disappear.

    The model can repair every bad slot or abstain from the entire batch. It
    cannot rewrite valid sibling claims, drop evidence, or silently remove an
    exclusion while repairing a protocol identifier. Nothing is persisted.
    """
    batch, normalized = normalize_column_references(batch, packet)
    if normalized:
        client.usage['column_references_normalized'] = client.usage.get('column_references_normalized', 0) + normalized
    slots = invalid_reference_slots(batch, packet)
    if not slots:
        return batch
    first_code = slots[0].code
    if len(slots) > 128:
        raise LightGraphError(first_code)
    redacted = batch.model_dump(mode='json')
    redacted['notes'] = ''; redacted['open_questions'] = []
    for proposal in redacted['proposals']:
        proposal['rationale'] = ''; proposal['requested_probes'] = []
    for index, slot in enumerate(slots):
        slot.put(redacted, f'UNRESOLVED_REFERENCE_{index}')
    diagnostics = [slot.diagnostic(index) for index, slot in enumerate(slots)]
    rejection = None
    try:
        for round_number in range(1, max_repairs + 1):
            client.budget.check_deadline()
            client.usage['reference_repair_attempts'] = client.usage.get('reference_repair_attempts', 0) + 1
            repair_packet = {**packet, 'reference_repair': {
                'round': round_number, 'diagnostics': diagnostics, 'candidate': redacted,
                'previous_rejection': rejection,
                'instruction': 'Repair identifier protocol only. Each UNRESOLVED_REFERENCE slot must be '
                    'replaced with an exact allowed_references ID supported by the original context. '
                    'Return exactly one replacement for EVERY listed slot_id, or abstain=true with no '
                    'replacements if a supported mapping is unavailable. Never invent references. '
                    'Claims, exclusions, valid references and business meaning cannot be changed. '
                    'Issued document section references use doc_id@revision:section_id. '
                    'A successful reference repair is not semantic confirmation.'}}
            try:
                repair = client.complete(packet['task'], repair_packet, ReferenceRepair)
                if repair.abstain and not repair.replacements:
                    client.usage['reference_repair_abstentions'] = client.usage.get('reference_repair_abstentions', 0) + 1
                    return ProposalBatch(task=batch.task, abstain=True)
                if repair.abstain or len(repair.replacements) != len(slots):
                    raise LightGraphError('reference_repair_invalid_patch')
                patches = {item.slot_id: item.reference for item in repair.replacements}
                if set(patches) != set(range(len(slots))):
                    raise LightGraphError('reference_repair_invalid_patch')
                # Apply atomically to a new copy after validating every patch.
                if any(patches[i] not in slot.allowed for i, slot in enumerate(slots)):
                    raise LightGraphError('reference_repair_unissued_reference')
                candidate = batch.model_dump(mode='json')
                for index, slot in enumerate(slots):
                    slot.put(candidate, patches[index])
                checked = ProposalBatch.model_validate(candidate)
                validate_proposal_references(checked, packet)
                client.usage['reference_repairs_succeeded'] = client.usage.get('reference_repairs_succeeded', 0) + 1
                return checked
            except ValidationError:
                # A permitted ID can still create a duplicate key or conflict.
                # Keep Pydantic's value-bearing error out of the next prompt.
                rejection = 'reference_repair_invalid_patch'
            except LightGraphError as exc:
                if exc.code not in {'llm_schema_invalid', 'reference_repair_invalid_patch',
                                    'reference_repair_unissued_reference', 'reference_repair_key_collision'}:
                    raise
                rejection = exc.code
            finally:
                repair_packet.clear()
        raise LightGraphError(first_code)
    finally:
        redacted.clear(); diagnostics.clear(); slots.clear()
