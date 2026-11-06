"""Validate and publish source-bound graph structure without enumerating facts.

    Structure records have their own manifest entries. They never stand in for
    instance validation or create business nodes merely because fields exist.
"""
from datetime import datetime, timezone
import uuid

from lightgraph.contracts import digest
from lightgraph.errors import LightGraphError
from lightgraph.query import QueryCompiler
from .access import scope_binding_map
from .executor import _projection_columns
from .models import GraphDraft
from .metadata_columns import is_metadata_mapping, metadata_endpoint, mapping_predicate, checked_column
from .quality import name_mapping_advice


def validate_structure(draft, tables, *, goal=None, dialects=None, scope_bindings=()):
    """Check authorized physical bindings and predicates using only the catalog."""
    draft = GraphDraft.model_validate(draft.model_dump(mode='python', by_alias=True)
                                     if isinstance(draft, GraphDraft) else draft)
    catalog = {(t.source_id, t.schema_name, t.name): t for t in tables}
    visibility = scope_binding_map(scope_bindings)
    diagnostics, bindings = [], []

    def check(mapping, columns, location):
        key = (mapping.source_id, mapping.schema_name, mapping.table)
        table = catalog.get(key)
        if table is None:
            raise LightGraphError('unauthorized_object')
        selected = list(dict.fromkeys(columns + visibility.get(key, [])))
        if not selected or set(selected) - set(table.column_names):
            raise LightGraphError('unauthorized_column')
        if goal is not None:
            from lightgraph.discovery.goal import column_is_evidence_only, column_is_excluded, source_in_goal
            if not source_in_goal(goal, table):
                raise LightGraphError('outside_source_goal')
            if any(column_is_excluded(goal, table, column) for column in selected):
                raise LightGraphError('excluded_column_projected')
            if any(column_is_evidence_only(goal, table, column) for column in selected):
                raise LightGraphError('evidence_only_column_projected')
            table = table.model_copy(update={'columns': [column for column in table.columns
                if not column_is_excluded(goal, table, column.name)]})
        compiler = QueryCompiler([table], dialect=(dialects or {}).get(table.source_id, 'postgres'))
        # Compiling a guarded query checks predicates too; it never executes it.
        compiler.projection(table, selected, predicate=mapping_predicate(mapping), limit=1)
        bindings.append({'source_id': table.source_id, 'schema': table.schema_name,
                         'table': table.name, 'columns': selected, 'path': location,
                         'predicate': mapping.predicate})

    if not draft.entities:
        diagnostics.append({'code': 'graph_draft_has_no_entities'})
    for category in ('entities', 'relations'):
        for index, definition in enumerate(getattr(draft, category)):
            allowed_kinds = getattr(goal, 'entity_kinds' if category == 'entities' else 'relation_kinds', [])
            if allowed_kinds and definition.type not in allowed_kinds:
                diagnostics.append({'code': 'outside_extraction_goal' if category == 'entities' else 'outside_relation_goal',
                                    'path': [category, index]})
                continue
            for number, mapping in enumerate(definition.mappings):
                path = [category, index, 'mappings', number]
                try:
                    if is_metadata_mapping(mapping):
                        checked_column(mapping, catalog, goal)
                        bindings.append({'source_id': mapping.source_id, 'schema': mapping.schema_name,
                            'table': mapping.table, 'columns': [mapping.column], 'path': path,
                            'source_kind': 'metadata_column', 'evidence_refs': mapping.evidence_refs,
                            'fact_queries': 0})
                    else:
                        check(mapping, _projection_columns(mapping), path)
                        _, endpoint = metadata_endpoint(mapping)
                        if endpoint is not None:
                            checked_column(endpoint, catalog, goal)
                except LightGraphError as exc:
                    diagnostics.append({'code': exc.code, 'path': path})
                for name in ('source_lookup', 'target_lookup'):
                    lookup = getattr(mapping, name, None)
                    if lookup is not None:
                        try:
                            check(lookup, list(dict.fromkeys(lookup.match_columns + lookup.key_columns)), path + [name])
                        except LightGraphError as exc:
                            diagnostics.append({'code': exc.code, 'path': path + [name]})
    return {'passed': not diagnostics, 'validation_level': 'structure', 'data_validation': 'not_run',
            'instance_enumeration': 'not_run', 'diagnostics': diagnostics,
            'quality_advice': name_mapping_advice(draft), 'bindings': bindings,
            'metrics': {'mappings_checked': sum(len(d.mappings) for d in draft.entities + draft.relations),
                        'fact_queries': 0}}


def save_structure_check(workspace, draft_id, tables, *, access, **options):
    record = workspace.get_draft(draft_id)
    if record.status == 'stale':
        raise LightGraphError('stale_graph_draft')
    result = validate_structure(record.draft, tables, **options)
    artifact = {'format_version': 1, 'target': 'graph_structure',
                'validation_id': 'structure_' + uuid.uuid4().hex,
                'draft_id': draft_id, 'revision': record.revision,
                'draft_fingerprint': record.fingerprint,
                'context_fingerprint': workspace.context_fingerprint,
                'access_fingerprint': access,
                'checked_at': datetime.now(timezone.utc).isoformat(),
                'semantic_origin': 'agent_authored',
                'draft': workspace._draft_record_payload(record)['draft'], **result}
    relative = workspace._append_record('structure-checks', artifact)
    manifest = workspace._manifest()
    entry = manifest.setdefault('structure_checks', {}).setdefault(draft_id, {'history': []})
    entry['history'].append({'path': relative, 'digest': digest(artifact)})
    entry['current'] = entry['history'][-1]
    workspace._save_manifest(manifest)
    return {'status': 'ok', 'draft_id': draft_id, 'revision': record.revision,
            'validation_id': artifact['validation_id'], 'structure_artifact': relative,
            'published': False, **result}


def publish_structure(workspace, draft_id, *, access):
    """Publish exactly the checked plan; it contains no materialized graph facts."""
    record = workspace.get_draft(draft_id)
    if record.status == 'stale':
        raise LightGraphError('stale_graph_draft')
    manifest = workspace._manifest()
    entry = manifest.get('structure_checks', {}).get(draft_id, {}).get('current')
    if not entry:
        raise LightGraphError('structure_validation_required')
    artifact = workspace._read_record(entry['path'])
    if digest(artifact) != entry['digest']:
        raise LightGraphError('validated_structure_mismatch')
    if (not artifact['passed'] or artifact['draft_fingerprint'] != record.fingerprint
            or artifact['revision'] != record.revision
            or artifact['context_fingerprint'] != workspace.context_fingerprint
            or artifact['access_fingerprint'] != access):
        raise LightGraphError('structure_validation_required')
    publication = {'draft_id': draft_id, 'revision': record.revision,
                   'context_fingerprint': workspace.context_fingerprint,
                   'draft_fingerprint': record.fingerprint,
                   'validation_id': artifact['validation_id'],
                   'path': workspace._append_record('structures', {**artifact, 'status': 'published'}),
                   'data_validation': 'not_run', 'instance_enumeration': 'not_run'}
    manifest.setdefault('structure_publications', {})[draft_id] = publication
    manifest.setdefault('structure_publication_history', []).append(publication)
    workspace._save_manifest(manifest)
    return {'status': 'ok', 'target': 'graph_structure', 'publication': publication,
            'business_nodes': 0, 'business_edges': 0, 'data_validation': 'not_run',
            'instance_enumeration': 'not_run'}


def structure_ready(workspace, record, *, discover_only=False):
    """Require a matching artifact, not merely an old publication count."""
    manifest = workspace._manifest()
    if record.status == 'stale':
        return False
    current = manifest.get('structure_checks', {}).get(record.draft.draft_id, {}).get('current')
    if not current:
        return False
    artifact = workspace._read_record(current['path'])
    if digest(artifact) != current['digest']:
        raise LightGraphError('validated_structure_mismatch')
    if (not artifact['passed'] or artifact['revision'] != record.revision
            or artifact['draft_fingerprint'] != record.fingerprint
            or artifact['context_fingerprint'] != workspace.context_fingerprint):
        return False
    if discover_only:
        return True
    publication = manifest.get('structure_publications', {}).get(record.draft.draft_id, {})
    if (publication.get('revision') != record.revision
            or publication.get('draft_fingerprint') != record.fingerprint
            or publication.get('context_fingerprint') != workspace.context_fingerprint
            or publication.get('validation_id') != artifact['validation_id']):
        return False
    published = workspace._read_record(publication['path'])
    return published == {**artifact, 'status': 'published'}
