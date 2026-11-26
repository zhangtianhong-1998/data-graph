"""Project frozen, program-issued unit grants onto a particular common physical table."""
from dataclasses import dataclass
from pathlib import Path

from lightgraph.contracts import digest
from lightgraph.errors import LightGraphError
from .snapshot_registry import _context_path, result_access_policy
from .workspace import read_internal_yaml


@dataclass(frozen=True)
class _CheckpointGrant:
    fingerprint: str
    policy: dict


def _attach(contributor, artifact, policy):
    graph = contributor.get('graph', {})
    fingerprint = graph.get('access_fingerprint')
    if (artifact.get('artifact_status') != 'committed' or artifact.get('node_id') != contributor['contributor_id']
            or artifact.get('contributor_kind') != 'unit' or artifact.get('graph_kind') != 'node_delta'
            or not fingerprint or artifact.get('access_fingerprint') != fingerprint
            or not isinstance(policy, dict) or policy.get('version') != 1
            or not policy.get('isolation_domain') or digest(policy) != artifact.get('result_policy_id')):
        return contributor
    return {**contributor, '_projection_grant': _CheckpointGrant(fingerprint, policy)}


def attach_checkpoint_grant(contributor, output_dir, artifact):
    """Use only the access record named by this verified checkpoint's original commit marker."""
    if '_projection_grant' in contributor:
        contributor = {k: v for k, v in contributor.items() if k != '_projection_grant'}
    if not artifact.get('result_policy_id'):
        return contributor
    try:
        path = _context_path(Path(output_dir) / 'construction', artifact.get('access_fingerprint'),
                             artifact['result_policy_id'])
        if not path.is_file():
            return contributor
        record = read_internal_yaml(path)
        if (not isinstance(record, dict) or record.get('format_version') != 1
                or record.get('access_fingerprint') != artifact.get('access_fingerprint')):
            return contributor
        return _attach(contributor, artifact, record.get('result_access_policy'))
    except (LightGraphError, OSError, ValueError):
        return contributor


def attach_runtime_grants(inputs, config, artifacts):
    """Re-derive effective unit grants, accepting only the exact frozen policy digest."""
    from .workflow import build_unit_config

    units = {u.unit_id: u for u in config.workflow.units} if config.workflow else {}
    enriched = []
    for contributor in inputs:
        if '_projection_grant' in contributor:
            contributor = {k: v for k, v in contributor.items() if k != '_projection_grant'}
        owner = contributor['contributor_id']
        record = artifacts.get(owner, {})
        if owner in units and record.get('result_policy_id'):
            scoped = build_unit_config(config, units[owner], unit_output_dir=config.output_dir)
            contributor = _attach(contributor, record, result_access_policy(scoped))
        enriched.append(contributor)
    return enriched


def common_table_grant(contributors, binding):
    """Equal result and full table privileges, with all unrelated table grants removed.

    Normalizing table selection cannot normalize credential revisions, isolation,
    document scopes, data permission, goal output restrictions or row scope bindings.
    The verifier must separately check that every observation used stays within
    this exact table/column/row boundary.
    """
    projected = []
    scope_columns = None
    table_columns = None
    qualified = binding.schema_name + '.' + binding.table
    for c in contributors:
        witness = c.get('_projection_grant')
        if not isinstance(witness, _CheckpointGrant) or witness.fingerprint != c['graph'].get('access_fingerprint'):
            return None
        policy = witness.policy
        if policy.get('inherited_graphs') or policy['isolation_domain'] != c.get('source_namespace'):
            return None
        sources = [source for source in policy['sources'] if source['id'] == binding.source_id]
        if len(sources) != 1:
            return None
        source, = sources
        tables = source['allowed_tables']
        if source.get('allow_fact_data') is False or (tables and qualified not in tables):
            return None
        if source['kind'] in ('mysql_database', 'gauss_database') and (
                not tables or source['allow_fact_data'] is not True):
            return None
        selections = source['allowed_columns'] or {}
        table_columns = selections.get(qualified)
        scopes = [b for b in policy['scope_bindings'] if b['source_id'] == binding.source_id
                  and b.get('schema', 'public') == binding.schema_name and b['table'] == binding.table]
        if len(scopes) > 1:
            return None
        scope_columns = scopes[0]['columns'] if scopes else []
        projected.append(digest({k: v for k, v in policy.items() if k not in ('sources', 'scope_bindings')},
                                {k: v for k, v in source.items() if k not in ('allowed_tables', 'allowed_columns')},
                                qualified, table_columns, scopes))
    if not projected or len(set(projected)) != 1:
        return None
    return tuple(scope_columns), frozenset(table_columns) if table_columns is not None else None


def checkpoint_observation_grant(contributor, items):
    """Check each source observation against its own frozen grant.

    Different table selections and document scopes are expected across workers.
    They do not authorize one worker to read another worker's observations.
    """
    from .identity_authority import source_address

    witness = contributor.get('_projection_grant')
    if not isinstance(witness, _CheckpointGrant) or witness.fingerprint != contributor['graph'].get('access_fingerprint'):
        return 'missing_frozen_observation_grant'
    policy = witness.policy
    if policy.get('inherited_graphs') or policy.get('isolation_domain') != contributor.get('source_namespace'):
        return 'observation_isolation_domain_mismatch'
    sources = {source['id']: source for source in policy['sources']}
    goal = policy.get('goal') or {}

    def restricted(column, address):
        sid, schema, table = address
        tokens = {column, f'{table}.{column}', f'{schema}.{table}.{column}',
                  f'{sid}.{schema}.{table}.{column}', f'{sid}:{schema}.{table}.{column}'}
        return bool(tokens.intersection(goal.get('excluded_columns', ()))
                    or tokens.intersection(goal.get('evidence_only_columns', ())))

    for item in items:
        if not item.get('supports'):
            return 'observation_support_missing'
        for support in item['supports']:
            prov = support.get('provenance') or {}
            address = source_address(prov.get('source'))
            if (address is None or support.get('inherited_from') or support.get('lookup_matches')
                    or prov.get('source_lookup') or prov.get('target_lookup')):
                return 'observation_source_unverified'
            sid, schema, table = address
            source = sources.get(sid)
            qualified = schema + '.' + table
            if source is None or source.get('allow_fact_data') is False:
                return 'observation_source_revoked'
            allowed_tables = source.get('allowed_tables') or []
            if (allowed_tables and qualified not in allowed_tables or source['kind'] in
                    ('mysql_database', 'gauss_database') and (not allowed_tables or source.get('allow_fact_data') is not True)):
                return 'observation_table_not_authorized'
            scopes = [b for b in policy['scope_bindings'] if b['source_id'] == sid
                      and b.get('schema', 'public') == schema and b['table'] == table]
            expected = scopes[0]['columns'] if len(scopes) == 1 else []
            if (len(scopes) > 1 or prov.get('scope_columns') != expected or not support.get('scope_ref')
                    or not expected and support['scope_ref'] != 'public'):
                return 'observation_scope_binding_mismatch'
            columns = prov.get('columns')
            if not isinstance(columns, list) or not columns:
                return 'observation_columns_unrecorded'
            used = set(expected)
            for field in ('key_columns', 'source_key_columns', 'target_key_columns', 'alias_columns', 'context_columns'):
                used.update(prov.get(field) or ())
            used.update((prov.get('property_columns') or {}).values())
            used.update((prov.get('business_context_mapping') or {}).get('columns', {}).values())
            used.update(value for field in ('label_column', 'description_column', 'english_name_column')
                        if (value := prov.get(field)))
            used.update((support.get('row_context') or {}).get('values', {}))
            if not used <= set(columns) | set(expected):
                return 'observation_columns_unrecorded'
            selections = source.get('allowed_columns') or {}
            allowed = selections.get(qualified)
            if allowed is not None and prov.get('predicate'):
                return 'observation_predicate_columns_unverified'
            if allowed is not None and not (used | set(columns)) <= set(allowed):
                return 'observation_columns_not_authorized'
            if any(restricted(column, address) for column in used | set(columns)):
                return 'observation_output_column_forbidden'
            allowed_types = goal.get('entity_kinds' if 'identity' in item else 'relation_kinds') or []
            if allowed_types and item.get('original_type', item.get('type')) not in allowed_types:
                return 'observation_type_not_authorized'
    return None
