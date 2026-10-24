"""Delete project inputs without touching source databases or published graph facts.

All callers hold the project lock and check the run/session boundary first.
References are prepared before any mutation; metadata is reconstructed only from
source-bound cached declarations, never by opening a database.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
import re
import shutil
import time
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

from ..catalog import CATALOG_VERSION, DeclaredCatalog, semantic_catalog
from ..contracts import digest
from ..documents.core import load_document
from ..errors import LightGraphError
from .workspace import _identifier, _no_symlinks

DB_KINDS = {'gauss_database', 'mysql_database'}


def delete_project(workspace, pid):
    path = workspace.project_path(pid)
    # rmtree must never traverse a user-injected link/junction outside the project.
    for root, directories, files in os.walk(path, followlinks=False):
        for name in directories + files:
            item = Path(root) / name
            _no_symlinks(item)
            if getattr(item, 'is_junction', lambda: False)():
                raise LightGraphError('workspace_symlink_forbidden')
    shutil.rmtree(path)
    return {'deleted': True, 'project_id': pid}


def _excel_sheet_names(path, *, include_hidden=False):
    """Read only bounded workbook declarations, never shared strings or cells."""
    limit = 2 * 1024 * 1024
    with zipfile.ZipFile(path) as archive:
        if archive.getinfo('xl/workbook.xml').file_size > limit:
            raise ValueError('workbook_metadata_too_large')
        with archive.open('xl/workbook.xml') as stream:
            xml = stream.read(limit + 1)
        if len(xml) > limit or b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():
            raise ValueError('workbook_metadata_invalid')
        root = ET.fromstring(xml)
        return [sheet.attrib['name'] for sheet in root.iter() if sheet.tag.split('}')[-1] == 'sheet'
                and (include_hidden or sheet.attrib.get('state', 'visible') == 'visible')]


def _names(target):
    sid, schema, name = target['source_id'], target.get('schema', ''), target.get('name', '')
    return {name, f'{schema}.{name}', f'{sid}:{schema}.{name}', f'{sid}.{schema}.{name}'} if name else set()


def scope_targets(scope, targets):
    """Preserve scopes rather than removing selectors and silently globalizing a doc."""
    for target in targets:
        sid = target['source_id']
        if scope.source_ids and sid not in scope.source_ids:
            continue
        if not target.get('name'):
            if sid in scope.source_ids or any(s.startswith((sid + '.', sid + ':')) for s in scope.tables + scope.columns):
                return True
        if any('.' not in column for column in scope.columns) and (not scope.tables or any(s in _names(target) for s in scope.tables)):
            return True
        if any(s in _names(target) for s in scope.tables):
            return True
        if any(s.startswith(prefix + '.') for prefix in _names(target) for s in scope.columns):
            return True
    return False


def document_targets(document, targets):
    return any(scope_targets(scope, targets) for scope in
               [document.scope, *(section.scope for section in document.sections if section.scope)])


def _rule_targets(value, targets):
    # RuleSpec has several nested SourceRefs, including relation endpoints and
    # evidence. Inspect structural references, never arbitrary labels/SQL text.
    if isinstance(value, list):
        return any(_rule_targets(item, targets) for item in value)
    if not isinstance(value, dict):
        return False
    if 'source_id' in value:
        for target in targets:
            if value['source_id'] == target['source_id'] and (not target.get('name') or
                    value.get('table') == target['name'] and value.get('schema', 'public') == target['schema']):
                return True
    return any(_rule_targets(item, targets) for item in value.values())


def _cached(workspace, pid):
    from .execution import _metadata_context
    path = workspace._path(pid, 'output/metadata.yaml')
    context_path = workspace._path(pid, 'output/metadata-context.json')
    if not path.exists() or not context_path.exists():
        return None, set()
    try:
        declared = DeclaredCatalog.model_validate(workspace._read(path)['declared'])
        cached_context = json.loads(context_path.read_text(encoding='utf-8'))
        current = _metadata_context(workspace.resolved_config(pid), workspace.project_path(pid))
        trusted = {sid for sid, revision in current['source_revisions'].items()
                   if cached_context.get('source_revisions', {}).get(sid) == revision}
        return declared, trusted
    except (LightGraphError, ValueError, TypeError, KeyError, OSError):
        return None, set()


def _metadata_update(workspace, pid, config, declared, trusted, targets):
    from ..runtime.config import RunConfig
    from .execution import _metadata_context
    resolved = RunConfig.model_validate(config).resolve_paths(workspace.project_path(pid))
    remaining = {s.id for s in resolved.sources}
    tables = [t for t in declared.tables if t.source_id in remaining & trusted and
              not any(t.source_id == target['source_id'] and t.schema_name == target.get('schema')
                      and t.name == target.get('name') for target in targets)] if declared else []
    sources = [s for s in declared.sources if s.source_id in remaining & trusted] if declared else []
    # A removed target is no longer an authorized endpoint in the preview.
    for table in tables:
        table.foreign_keys = [fk for fk in table.foreign_keys if not any(
            table.source_id == target['source_id'] and fk.target_schema == target.get('schema')
            and fk.target_table == target.get('name') for target in targets)]
    fresh = DeclaredCatalog(tables=tables, sources=sources,
        revision=digest(CATALOG_VERSION, [t.model_dump(by_alias=True) for t in tables], [s.model_dump() for s in sources]))
    semantic = semantic_catalog(fresh, resolved.goal, documents=[])
    return {
        workspace._path(pid, 'output/metadata.yaml'): yaml.safe_dump({
            'declared': fresh.model_dump(mode='json', by_alias=True),
            'semantic_annotations': semantic.model_dump(mode='json', by_alias=True)}, allow_unicode=True, sort_keys=False).encode(),
        workspace._path(pid, 'output/metadata-context.json'): json.dumps(
            _metadata_context(resolved, workspace.project_path(pid)), ensure_ascii=False).encode(),
    }


def delete_input(workspace, pid, *, fid=None, sid=None, table=None):
    project = workspace.get(pid)
    config = workspace.get_config(pid)
    original_project, original_config = deepcopy(project), deepcopy(config)
    declared, trusted = _cached(workspace, pid)
    removed_files = []
    removed_sources = []
    targets = []
    source = None
    if fid is not None:
        _identifier(fid)
        file = next((f for f in project['files'] if f['file_id'] == fid), None)
        if file is None:
            raise LightGraphError('project_file_not_found')
        if any(s.get('capability_manifest') == file['relative_path'] for s in config['sources']):
            raise LightGraphError('file_in_use')
        removed_files = [file]
        removed_sources = [s for s in config['sources'] if s.get('path') == file['relative_path']]
    else:
        source = next((s for s in config['sources'] if s['id'] == sid), None)
        if source is None:
            raise LightGraphError('source_not_found')
        if table is not None:
            if source['kind'] not in DB_KINDS:
                raise LightGraphError('database_source_required')
            if table not in source['allowed_tables']:
                raise LightGraphError('source_table_not_found')
            parts = table.split('.')
            if len(parts) != 2 or not all(parts):
                raise LightGraphError('invalid_table_name')
            targets.append({'source_id': sid, 'schema': parts[0], 'name': parts[1]})
            source['allowed_tables'] = [t for t in source['allowed_tables'] if t != table]
            if source.get('allowed_columns'):
                source['allowed_columns'].pop(table, None)
            if not source['allowed_tables']:
                removed_sources = [source]
        else:
            removed_sources = [source]
            # Removing a file-backed source also removes its upload only when
            # no other configured source uses that same file.
            if source.get('path') and not any(s['id'] != sid and s.get('path') == source['path'] for s in config['sources']):
                removed_files = [f for f in project['files'] if f['relative_path'] == source['path']]
    removed_ids = {s['id'] for s in removed_sources}
    for source in removed_sources:
        targets.append({'source_id': source['id'], 'schema': '', 'name': ''})
        names = [tuple(name.split('.', 1)) for name in source.get('allowed_tables', []) if '.' in name]
        if source['kind'] == 'excel_workbook' and source.get('path'):
            # Sheet names are declarations in an uploaded file; no cell samples
            # or database queries are needed to resolve legacy short doc scopes.
            try:
                names.extend(('public', name) for name in _excel_sheet_names(
                    workspace._path(pid, source['path']), include_hidden=source.get('include_hidden', False)))
            except (OSError, ValueError, KeyError, zipfile.BadZipFile, ET.ParseError):
                # A malformed upload must remain deletable. Deactivate scoped
                # docs conservatively when its sheet declarations are unknown.
                for file in project['files']:
                    if file['kind'] == 'markdown':
                        document = load_document(workspace.file_path(pid, file['file_id']))
                        for scope in [document.scope, *(section.scope for section in document.sections if section.scope)]:
                            if not scope.source_ids or source['id'] in scope.source_ids:
                                for selector in scope.tables:
                                    selector = selector.removeprefix(source['id'] + ':').removeprefix(source['id'] + '.')
                                    names.append(tuple(selector.split('.', 1)) if '.' in selector else ('public', selector))
        if source['kind'] == 'csv_file':
            names.append((source.get('schema_name', 'public'), source.get('table_name') or Path(source['path']).stem))
        if declared:
            names.extend((t.schema_name, t.name) for t in declared.tables if t.source_id == source['id'])
        for schema, name in set(names):
            targets.append({'source_id': source['id'], 'schema': schema, 'name': name})
    paths = {f['relative_path'] for f in removed_files}
    config['sources'] = [s for s in config['sources'] if s['id'] not in removed_ids]
    config['documents'] = [p for p in config['documents'] if p not in paths]
    detached_rules = [p for p in config['rules'] if p in paths]
    config['rules'] = [p for p in config['rules'] if p not in paths]
    for path in list(config['rules']):
        if _rule_targets(workspace._read(workspace._path(pid, path)), targets):
            config['rules'].remove(path)
            detached_rules.append(path)
    # Detach dependent rule *files* as a unit, preserving their uploaded text.
    # Cross-file endpoint references must not survive an absent producer rule.
    rule_files = {path: workspace._read(workspace._path(pid, path)) for path in original_config['rules']}
    def records(value):
        return value.get('rules', []) if isinstance(value, dict) else value if isinstance(value, list) else []
    def rule_refs(value):
        if isinstance(value, list):
            return set().union(*(rule_refs(item) for item in value)) if value else set()
        if not isinstance(value, dict):
            return set()
        refs = {v for k, v in value.items() if k in {'source_rule_id', 'target_rule_id'} and isinstance(v, str)}
        for item in value.values():
            refs.update(rule_refs(item))
        return refs
    while True:
        removed_rules = {r['rule_id'] for path in detached_rules for r in records(rule_files.get(path))
                         if isinstance(r, dict) and isinstance(r.get('rule_id'), str)}
        dependents = [path for path in config['rules'] if rule_refs(rule_files[path]) & removed_rules]
        if not dependents:
            break
        detached_rules.extend(dependents)
        config['rules'] = [path for path in config['rules'] if path not in dependents]
    if config.get('agent_config') in paths:
        config['agent_config'] = None
        # Removing a model configuration must not silently activate a fallback model.
        config['model'] = {'mode': 'disabled'}
    project['files'] = [f for f in project['files'] if f['relative_path'] not in paths]
    inactive_ids = []
    for file in project['files']:
        if file['kind'] != 'markdown':
            continue
        doc = load_document(workspace.file_path(pid, file['file_id']))
        if document_targets(doc, targets):
            file['inactive_reason'] = 'binding_target_removed'
            previous = file.get('inactive_targets', [])
            file['inactive_targets'] = deepcopy(previous + [t for t in targets if t not in previous])
            if file['relative_path'] in config['documents']:
                config['documents'].remove(file['relative_path'])
            inactive_ids.append(file['file_id'])
    clean = workspace._normalize_config(pid, config, project)
    writes = _metadata_update(workspace, pid, clean, declared, trusted, targets)
    deletions = [workspace._path(pid, f['relative_path']) for f in removed_files]
    deletions.extend(workspace._path(pid, f"metadata/{f['file_id']}.yaml") for f in removed_files)
    for removed in removed_sources:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', removed['id']):
            # Imported configurations may use broader IDs. The credential store
            # never accepts these as filenames, so no profile can exist for them.
            continue
        deletions.append(workspace._path(pid, f".connections/{removed['id']}.enc"))
    changed_ids = removed_ids | ({sid} if table is not None else set())
    from .saved_connections import SavedConnections
    profiles = SavedConnections(workspace.root)
    def load_draft(key):
        try:
            return profiles.load_draft(pid, key)
        except LightGraphError as exc:
            if exc.code not in {'saved_connection_corrupt', 'saved_connection_key_missing'}:
                raise
            return None
    draft_keys = ['source-' + changed for changed in sorted(changed_ids)
                  if re.fullmatch(r'[A-Za-z0-9_-]{1,128}', changed)]
    new_draft = load_draft('new') if changed_ids else None
    if new_draft and new_draft['draft']['form']['id'] in changed_ids:
        draft_keys.append('new')
    for key in draft_keys:
        current = load_draft(key)
        path = workspace._path(pid, '.connection-drafts/' + key + '.enc')
        marker = path.with_suffix('.rev')
        _no_symlinks(marker)
        try:
            previous = profiles._draft_tombstone(path) or 0
        except LightGraphError as exc:
            if exc.code != 'saved_connection_corrupt':
                raise
            previous = 0
        # Keep a nonsecret deletion marker. Normal browser revisions use epoch
        # milliseconds; a late blur/unload request cannot restore old selections.
        revision = max(previous, (current['revision'] + 1) if current else 0, int(time.time() * 1000))
        writes[marker] = str(revision).encode('ascii')
        deletions.append(path)
    # Validate every target before committing. In particular, never follow a
    # symlink planted in metadata or encrypted credential directories.
    for path in [*writes, *deletions]:
        _no_symlinks(path)
        if path.exists() and not path.is_file():
            raise LightGraphError('workspace_path_forbidden')
    backups = {p: p.read_bytes() if p.exists() else None for p in [*writes, *deletions]}
    try:
        workspace._write_yaml(workspace._path(pid, 'config.yaml'), clean)
        workspace._save(project)
        for path, value in writes.items():
            path.parent.mkdir(mode=0o700, exist_ok=True)
            workspace._atomic(path, value)
        for path in deletions:
            path.unlink(missing_ok=True)
    except Exception:
        workspace._write_yaml(workspace._path(pid, 'config.yaml'), original_config)
        workspace._save(original_project)
        for path, value in backups.items():
            if value is None:
                path.unlink(missing_ok=True)
            else:
                workspace._atomic(path, value)
        raise
    return {'deleted': True, 'removed_source_ids': sorted(removed_ids),
            'removed_file_ids': [f['file_id'] for f in removed_files],
            'inactive_document_ids': inactive_ids, 'detached_rule_paths': detached_rules,
            'project': project, 'config': clean}


def validate_rebinding(workspace, pid, document):
    """Reactivate only when all document/section targets are known to exist."""
    from ..documents.core import DocumentCorpus
    declared, trusted = _cached(workspace, pid)
    sources = {s['id'] for s in workspace.get_config(pid)['sources']}
    scopes = [document.scope, *(s.scope for s in document.sections if s.scope)]
    for scope in scopes:
        if not set(scope.source_ids) <= sources:
            raise LightGraphError('invalid_document_source_binding')
    if declared and trusted >= sources:
        DocumentCorpus([document]).bind_catalog(declared.tables)
    elif any(scope.tables or scope.columns for scope in scopes):
        raise LightGraphError('document_binding_requires_metadata')
