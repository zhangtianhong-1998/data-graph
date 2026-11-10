"""Derive a separately verifiable column-definition draft from saved work.

This selects existing Agent-authored mappings. It does not infer new semantics,
promote partial fact observations, or alter the parent revision.
"""
from lightgraph.contracts import digest
from lightgraph.errors import LightGraphError

from .metadata_columns import is_metadata_mapping
from .models import DraftRecord, GraphDraft


def _metadata_entities(record):
    return [entity for entity in record.draft.entities
            if all(is_metadata_mapping(mapping) for mapping in entity.mappings)]


def definition_options(record: DraftRecord) -> dict:
    """Offer an explicit save operation; no child is saved or published here."""
    entities = _metadata_entities(record)
    if record.status == 'stale':
        return {'available': False, 'reason': 'stale_graph_draft'}
    if not entities:
        return {'available': False, 'reason': 'metadata_definitions_unavailable'}
    if len(entities) == len(record.draft.entities) and not record.draft.relations:
        return {'available': False, 'reason': 'already_metadata_only'}
    return {
        'available': True,
        'parent_draft_id': record.draft.draft_id,
        'parent_revision': record.revision,
        'entity_types': len(entities),
        'column_definitions': sum(len(entity.mappings) for entity in entities),
        'tool': 'save_graph_draft',
        'arguments': {'metadata_from_draft': record.draft.draft_id},
        'next_action': 'Save these existing catalog-column definitions as an independent draft, '
            'then validate and publish that draft in instances mode. No fact queries are needed '
            'for these objects. This does not validate dimension members or indicator relations. '
            'Keep the parent revision and its unmet fact requirements; catalog authorization '
            'and evidence must still be valid.',
    }


def _append_note(items, note):
    # Preserve every existing note even at the schema's maximum list length.
    values = list(items)
    if len(values) == 128:
        values[-1] += '\n' + note
    else:
        values.append(note)
    return values


def metadata_definition_draft(record: DraftRecord) -> GraphDraft:
    """Retain exact column mappings/evidence under a revision-specific child ID.

    Every current relation mapping requires a fact-row endpoint. Consequently
    none belongs in a catalog-only draft, even if its other endpoint is a column.
    The full source revision remains the authoritative record of omitted work.
    """
    if record.status == 'stale':
        raise LightGraphError('stale_graph_draft')
    entities = _metadata_entities(record)
    if not entities:
        raise LightGraphError('metadata_definitions_unavailable')
    parent = record.draft
    if len(entities) == len(parent.entities) and not parent.relations:
        raise LightGraphError('metadata_definition_draft_not_needed')
    lineage = {'draft_id': parent.draft_id, 'revision': record.revision,
               'fingerprint': record.fingerprint, 'context_fingerprint': record.context_fingerprint}
    identifier = parent.draft_id[:40] + '_metadata_' + digest(lineage)[:16]
    decisions = _append_note(parent.decisions,
        f'Column-definition subset of draft {parent.draft_id} revision {record.revision}; '
        f'parent fingerprint={record.fingerprint}; context={record.context_fingerprint}. '
        'The existing metadata_column mappings and their evidence are copied without '
        'semantic changes. No fact members or fact-row relations are asserted here. '
        f'Original purpose: {parent.purpose}')
    unresolved = list(parent.unresolved)
    excluded_entities = [entity.type for entity in parent.entities if entity not in entities]
    if excluded_entities or parent.relations:
        # Excluded work belongs to the retained parent. Making its scope a new
        # child requirement would keep the run incomplete even after that
        # parent is repaired. Preserve genuine existing gaps above instead.
        decisions = _append_note(decisions,
            f'Draft {parent.draft_id} revision {record.revision} retains '
            f'{len(excluded_entities)} fact-entity types and {len(parent.relations)} relation types '
            'outside this column-definition publication. Their verification and original goal '
            'coverage must be determined from the parent and independent validations; '
            'publishing this subset does not resolve them.')
    return GraphDraft(draft_id=identifier,
        purpose='Publish the existing catalog-column definitions only; fact members and relations are outside this subset.',
        entities=[entity.model_copy(deep=True) for entity in entities], relations=[],
        evidence_refs=list(parent.evidence_refs), rationale=parent.rationale,
        decisions=decisions, unresolved=unresolved, suggestions=list(parent.suggestions))
