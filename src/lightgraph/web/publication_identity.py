"""Human review pinned to the current immutable construction publication.

Each change replays the active decisions against the same originals. The single
manifest replacement publishes the graph, the decision ledger and their shared
version together. History retains both the original graph and all prior views.
"""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from lightgraph.construction.access import filter_graph_scopes
from lightgraph.construction.domain_variant_candidates import domain_variant_candidates
from lightgraph.construction.graph_storage import write_graph_record
from lightgraph.construction.human_identity import Decisions, candidate_key, check_review_scope, project
from lightgraph.construction.snapshot_registry import append_graph_head, authorize_snapshot, snapshot_id
from lightgraph.construction.workspace import read_internal_yaml, write_yaml
from lightgraph.contracts import digest
from lightgraph.errors import LightGraphError
from lightgraph.runtime.locking import run_lock
from .construction_graph import _record_path, read_publication_record
from .graph_history import Entry, _authorize_entry


class Review:
    def __init__(self, workspace, pid, config, version, business_context):
        self.root = Path(config.output_dir) / 'construction'
        self.manifest_path = _record_path(self.root, 'manifest.yaml')
        self.manifest = read_internal_yaml(self.manifest_path)
        self.head = self.manifest['current_graph']
        self.version = snapshot_id(self.head, root=self.root)
        if version is not None and version != self.version:
            raise LightGraphError('resolution_version_conflict')
        self.base = self.head
        self.base_id = self.version
        self.ledger = {'revision': 0, 'decisions': []}
        self.authorize(workspace, config)
        previous = self.head.get('identity_review')
        if previous:
            self.base_id = previous['base_version_id']
            self.base = next((h for h in self.manifest.get('graph_history', [])
                              if snapshot_id(h, root=self.root) == self.base_id), None)
            if self.base is None:
                raise LightGraphError('graph_version_not_found')
            self.authorize(workspace, config)
            self.ledger = read_internal_yaml(_record_path(self.root, previous['ledger_path']))
            if digest(self.ledger) != previous['ledger_digest']:
                raise LightGraphError('resolution_ledger_conflict')
        compose = bool(config.inherited_graphs and self.base.get('graph_kind') not in {
            'project_full', 'node_full', 'node_delta', 'node_base'})
        summary = self.base.get('inherited_summary')
        if compose and 'inherited_summary' not in self.base:
            # Old non-self-contained deltas lack an immutable witness for the inherited inputs.
            # A retroactive review could otherwise replay against a later frozen base.
            raise LightGraphError('resolution_inherited_delta_conflict')
        self.raw = read_publication_record(self.root, self.base, config, compose_base=bool(compose and summary),
            pinned_summary=summary, policy_record=authorize_snapshot(self.root, self.base, config))
        self.visible = filter_graph_scopes({k: v for k, v in self.raw.items() if k != '_checkpoint_evidence_records'},
                                           config.scopes, business_context=business_context)
        self.nodes = {n['id']: n for n in self.visible['nodes']}
        self.state = Decisions(self.ledger['decisions'])
        self.groups = {}
        # Older publications may hold two domain variants without a recorded candidate.
        # Derive review suggestions from this reader's visible base witnesses so GET,
        # submit and revoke all use the same immutable member IDs.
        stored = self.visible.get('candidate_identity_merges', [])
        existing = {frozenset(group['node_ids']) for group in stored}
        local_workspace = (self.base.get('graph_kind') == 'workspace_publication' and not config.inherited_graphs
                           and not self.base.get('inherited_summary') and not self.raw.get('extends_base_graph'))
        suggestions = [group for group in domain_variant_candidates(self.visible, local_workspace=local_workspace)
                       if frozenset(group['node_ids']) not in existing]
        for group in [*stored, *suggestions]:
            members = sorted(self.state.expanded(group['node_ids']))
            if set(members) <= self.nodes.keys():
                self.groups[candidate_key(self.base_id, group)] = (members, group)

    def authorize(self, workspace, config):
        for head in (self.head, self.base) if self.head is not self.base else (self.head,):
            _authorize_entry(workspace, Entry(self.root, self.root, head, {}), config)

    def member_nodes(self, members):
        from .graph_payload import canvas_item
        return [canvas_item({**self.nodes[m], 'kind': self.nodes[m]['type'], 'status': 'accepted'}) for m in members]

    def candidate(self, key, members, group, incident):
        edges = {edge['id']: edge for nid in members for edge in incident.get(nid, [])}
        observations = [self.nodes[member] for member in members] + list(edges.values())
        facts, supports = [], []
        total_supports = 0
        for item in observations:
            edge = 'source' in item and 'target' in item
            if len(facts) < 500:
                payload = ({'source': item['source'], 'target': item['target'], 'relation': item['type']}
                           if edge else {'entity_id': item['id'], 'kind': item['type'], 'label': item['label']})
                facts.append({'id': item['id'], 'kind': 'Edge' if edge else 'NodeExistence', 'payload': payload})
            for support in item['supports']:
                total_supports += 1
                if len(supports) < 500:
                    supports.append({**support, 'fact_id': item['id']})
        reason = group.get('reason') or 'candidate'
        channel = reason
        if reason.startswith('same_label_'):
            channel = 'normalized_label'
        elif reason == 'cross_unit_unverified_identity_domain':
            channel = 'same_typed_identity'
        return {'candidate_id': key, 'members': members, 'original_members': group['node_ids'],
                'nodes': self.member_nodes(members), 'reason': group.get('reason'), 'status': 'candidate',
                'channels': [channel],
                'evidence': [fact['id'] for fact in facts],
                'has_active_decision': self.state.resolved(members), 'facts': facts, 'supports': supports,
                'facts_truncated': len(observations) > len(facts),
                'supports_truncated': total_supports > len(supports)}


def review(workspace, pid, *, config, offset, decision_offset, version, business_context):
    session = Review(workspace, pid, config, version, business_context)
    groups = list(session.groups.items())
    incident = {}
    for edge in session.visible['edges']:
        for member in {edge['source'], edge['target']}:
            incident.setdefault(member, []).append(edge)
    candidates = [session.candidate(key, members, group, incident)
                  for key, (members, group) in groups[offset:offset + 50]]
    decisions = [d for d in reversed(session.ledger['decisions'])
                 if d['candidate_id'] in session.groups and set(d['members']) <= session.nodes.keys()]
    rows = [{**d, 'active': d['status'] == 'active', 'nodes': session.member_nodes(d['members'])}
            for d in decisions[decision_offset:decision_offset + 50]]
    return {'backend': 'publication', 'revision': session.ledger['revision'], 'version_id': session.version,
            'base_version_id': session.base_id, 'candidates': candidates, 'decisions': rows,
            'total_candidates': len(groups), 'next_offset': offset + 50 if offset + 50 < len(groups) else None,
            'total_decisions': len(decisions),
            'next_decision_offset': decision_offset + 50 if decision_offset + 50 < len(decisions) else None}


def change(workspace, pid, config, *, candidate_id, verdict, decision_id, expected_revision, version_id,
           members, business_context):
    if not version_id:
        raise LightGraphError('resolution_version_conflict')
    with run_lock(config.output_dir):
        session = Review(workspace, pid, config, version_id, business_context)
        ledger = session.ledger
        if expected_revision != ledger['revision']:
            raise LightGraphError('resolution_revision_conflict')
        next_revision = expected_revision + 1
        now = datetime.now(timezone.utc).isoformat()
        if decision_id:
            decision = next((d for d in ledger['decisions'] if d['decision_id'] == decision_id
                             and d['candidate_id'] in session.groups and set(d['members']) <= session.nodes.keys()), None)
            if decision is None:
                raise LightGraphError('identity_decision_not_found')
            if decision['status'] != 'active':
                raise LightGraphError('resolution_revision_conflict')
            check_review_scope(session.raw, session.visible, decision['members'], merge=False)
            decision.update(status='revoked', revoked_revision=next_revision, revoked_at=now)
        else:
            if candidate_id not in session.groups:
                raise LightGraphError('identity_candidate_not_found')
            expanded, _ = session.groups[candidate_id]
            chosen = sorted(members if members is not None else expanded)
            if (not 2 <= len(chosen) <= 256 or len(chosen) != len(set(chosen)) or not set(chosen).issubset(expanded)
                    or verdict not in ('same', 'different')):
                raise LightGraphError('invalid_resolution_members')
            if verdict == 'same' and session.state.expanded(chosen) != set(chosen):
                raise LightGraphError('resolution_cluster_review_required')
            check_review_scope(session.raw, session.visible, chosen, merge=verdict == 'same')
            # A contrary verdict is tested against all prior decisions during replay.
            if session.state.resolved(chosen):
                raise LightGraphError('resolution_decision_conflict')
            decision = {'decision_id': 'pd_' + uuid4().hex, 'candidate_id': candidate_id,
                        'members': chosen, 'origin': 'user', 'status': 'active',
                        'verdict': verdict, 'revision': next_revision, 'created_at': now}
            ledger['decisions'].append(decision)
        result = project(session.raw, ledger['decisions'], session.base_id)
        result.update(storage_layout='parquet_v1', access_fingerprint=session.base['access_fingerprint'])
        ledger['revision'] = next_revision
        publication = 'published/' + uuid4().hex + '.graph'
        path = _record_path(session.root, publication)
        write_graph_record(path, result)
        ledger_path = publication + '/identity-decisions.yaml'
        write_yaml(_record_path(session.root, ledger_path), ledger)
        old = session.head
        head = {k: deepcopy(v) for k, v in old.items() if k not in (
            'created_at', 'version_id', 'publication_digest', 'path', 'graph_digest', 'identity_review')}
        head.update(path=publication, created_at=now, graph_digest=read_internal_yaml(path / 'metadata.yaml')['graph_digest'],
                    storage_layout='parquet_v1', identity_review={'base_version_id': session.base_id,
                    'parent_version_id': session.version, 'ledger_path': ledger_path, 'ledger_digest': digest(ledger)})
        # The caller holds the project lock and this transaction holds the output writer lease.
        # Recheck external source grants and the active generation after building the snapshot.
        if workspace.result_config(pid) != config:
            raise LightGraphError('resolution_version_conflict')
        session.authorize(workspace, config)
        manifest = read_internal_yaml(session.manifest_path)
        if snapshot_id(manifest['current_graph'], root=session.root) != session.version:
            raise LightGraphError('resolution_version_conflict')
        history = manifest.setdefault('graph_history', [])
        if not any(snapshot_id(h, root=session.root) == session.version for h in history):
            history.append(old)
        manifest['current_graph'] = append_graph_head(manifest, head, root=session.root)
        write_yaml(session.manifest_path, manifest)
        return {**decision, 'revision': next_revision, 'version_id': head['version_id'], 'backend': 'publication'}
