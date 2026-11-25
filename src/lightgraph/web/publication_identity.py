"""Human review pinned to the current immutable construction publication.

Each change replays the active decisions against the same originals. The single
manifest replacement publishes the graph, the decision ledger and their shared
version together. History retains both the original graph and all prior views.
"""
from contextlib import contextmanager
from copy import deepcopy
from functools import cached_property
from itertools import islice
import logging
import shutil
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
from .construction_graph import _record_path, load_prepared, read_publication_record
from .graph_history import Entry, _authorize_entry


class Review:
    def __init__(self, workspace, pid, config, version, business_context, *, editable=False):
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
        def index(visible):
            local_workspace = (self.base.get('graph_kind') == 'workspace_publication' and not config.inherited_graphs
                               and not summary and not visible.get('extends_base_graph'))
            return CandidateIndex(visible, self.base_id, Decisions(self.ledger['decisions']), local_workspace)

        if editable:
            self.raw = read_publication_record(self.root, self.base, config, compose_base=bool(compose and summary),
                pinned_summary=summary, policy_record=authorize_snapshot(self.root, self.base, config))
            self.visible = filter_graph_scopes({k: v for k, v in self.raw.items() if k != '_checkpoint_evidence_records'},
                                               config.scopes, business_context=business_context)
            self.index = index(self.visible)
        else:
            # Reuse the canvas's permission-checked immutable base view. Both heads and the
            # ledger above are still authorized/verified on every read, including cache hits.
            graph, _ = load_prepared(config, root=self.root, head=self.base)
            key = (self.version, (previous or {}).get('ledger_digest'), digest(business_context))
            cached = getattr(graph, 'review_index', None)
            if cached is not None and cached[0] == key:
                self.index = cached[1]
            else:
                visible = (graph if business_context is None else
                           filter_graph_scopes(graph, config.scopes, business_context=business_context, copy_supports=False))
                self.index = index(visible)
                # At most one derived index per resident base view. Changing scope, publication
                # or file stamps replaces the outer view; context/ledger changes replace this index.
                graph.review_index = (key, self.index)

    def authorize(self, workspace, config):
        for head in (self.head, self.base) if self.head is not self.base else (self.head,):
            _authorize_entry(workspace, Entry(self.root, self.root, head, {}), config)


class CandidateIndex:
    def __init__(self, graph, base_id, state, local_workspace):
        self.nodes = {n['id']: n for n in graph['nodes']}
        self.edges = graph['edges']
        self.state = state
        self.groups = {}
        stored = graph.get('candidate_identity_merges', [])
        existing = {frozenset(group['node_ids']) for group in stored}
        suggestions = [group for group in domain_variant_candidates(graph, local_workspace=local_workspace)
                       if frozenset(group['node_ids']) not in existing]
        for group in [*stored, *suggestions]:
            members = sorted(state.expanded(group['node_ids']))
            if set(members) <= self.nodes.keys():
                self.groups[candidate_key(base_id, group)] = (members, group)

    @cached_property
    def incident(self):
        lookup = {}
        for edge in self.edges:
            for member in {edge['source'], edge['target']}:
                lookup.setdefault(member, []).append(edge)
        return lookup

    def member_nodes(self, members):
        from .graph_payload import canvas_item
        return [canvas_item({**self.nodes[m], 'kind': self.nodes[m]['type'], 'status': 'accepted'}) for m in members]

    def candidate(self, key, members, group, *, summary):
        reason = group.get('reason') or 'candidate'
        channel = reason
        if reason.startswith('same_label_'):
            channel = 'normalized_label'
        elif reason == 'cross_unit_unverified_identity_domain':
            channel = 'same_typed_identity'
        row = {'candidate_id': key, 'reason': group.get('reason'), 'status': 'candidate',
               'channels': [channel], 'has_active_decision': self.state.resolved(members)}
        if summary:
            return {**row, 'member_count': len(members),
                    'nodes': [member_caption(self.nodes[m]) for m in members[:3]]}
        edges = {edge['id']: edge for nid in members for edge in self.incident.get(nid, [])}
        observations = [self.nodes[member] for member in members] + list(edges.values())
        facts, supports = [], []
        total_supports = 0
        for item in observations:
            edge = 'source' in item and 'target' in item
            if len(facts) < 500:
                payload = ({'source': item['source'], 'target': item['target'], 'relation': item['type']}
                           if edge else {'entity_id': item['id'], 'kind': item['type'], 'label': item['label']})
                facts.append({'id': item['id'], 'kind': 'Edge' if edge else 'NodeExistence', 'payload': payload})
            total_supports += len(item['supports'])
            supports.extend({**support, 'fact_id': item['id']} for support in item['supports'][:500 - len(supports)])
        return {**row, 'members': members, 'original_members': group['node_ids'], 'nodes': self.member_nodes(members),
                'evidence': [fact['id'] for fact in facts], 'facts': facts, 'supports': supports,
                'facts_truncated': len(observations) > len(facts), 'supports_truncated': total_supports > len(supports)}


def member_caption(node):
    """Bound text in the list; the selected group's detail retains the complete identities."""
    label = str(node.get('label') or node['id'])
    result = {'id': node['id'], 'label': label if len(label) <= 128 else label[:128] + '…',
              'kind': node.get('type', node.get('kind'))}
    identity = node.get('identity')
    if isinstance(identity, dict):
        result['identity'] = {key[:80]: (value[:96] + '…' if isinstance(value, str) and len(value) > 96 else value)
                              for key, value in islice(identity.items(), 3) if isinstance(key, str) and isinstance(value, (str, int, float, bool))}
    return result


def review(workspace, pid, *, config, offset, decision_offset, version, business_context, view='full', candidate_id=None):
    session = Review(workspace, pid, config, version, business_context)
    index = session.index
    if candidate_id is not None:
        if candidate_id not in index.groups:
            raise LightGraphError('identity_candidate_not_found')
        items = [(candidate_id, index.groups[candidate_id])]
    else:
        items = islice(index.groups.items(), min(offset, len(index.groups)), min(offset + 50, len(index.groups)))
    candidates = [index.candidate(key, members, group, summary=view == 'summary') for key, (members, group) in items]
    decisions = [d for d in reversed(session.ledger['decisions'])
                 if d['candidate_id'] in index.groups and set(d['members']) <= index.nodes.keys()]
    rows = []
    for decision in decisions[decision_offset:decision_offset + 50]:
        members = decision['members']
        nodes = ([member_caption(index.nodes[m]) for m in members[:3]] if view == 'summary'
                 else index.member_nodes(members))
        rows.append({**decision, 'active': decision['status'] == 'active', 'nodes': nodes})
    # Selection pins the current publication. A competing decision committed while reading
    # must not return an old ledger as the current state, even when its base still exists.
    if config.inherited_graphs:
        session.authorize(workspace, config)
    head = read_internal_yaml(session.manifest_path)['current_graph']
    if snapshot_id(head, root=session.root) != session.version:
        raise LightGraphError('resolution_version_conflict')
    return {'backend': 'publication', 'revision': session.ledger['revision'], 'version_id': session.version,
            'base_version_id': session.base_id, 'candidates': candidates, 'decisions': rows,
            'total_candidates': len(index.groups), 'next_offset': offset + 50 if offset + 50 < len(index.groups) else None,
            'total_decisions': len(decisions),
            'next_decision_offset': decision_offset + 50 if decision_offset + 50 < len(decisions) else None}


@contextmanager
def _pending_review_graph(session, publication, graph):
    """Remove only this writer's unreferenced snapshot if a review cannot commit."""
    path = _record_path(session.root, publication)
    write_graph_record(path, graph)
    try:
        yield path
    except BaseException:
        try:
            manifest = read_internal_yaml(session.manifest_path)
            heads = [manifest.get('current_graph') or {}, *manifest.get('graph_history', [])]
            if not any(head.get('path') == publication for head in heads):
                shutil.rmtree(path)
        except (OSError, LightGraphError, ValueError, TypeError, AttributeError):
            # If the commit cannot be checked, preserve the record and original error.
            logging.getLogger(__name__).warning('Could not verify unpublished identity review snapshot')
        raise


def change(workspace, pid, config, *, candidate_id, verdict, decision_id, expected_revision, version_id,
           members, business_context):
    if not version_id:
        raise LightGraphError('resolution_version_conflict')
    with run_lock(config.output_dir):
        session = Review(workspace, pid, config, version_id, business_context, editable=True)
        ledger = session.ledger
        if expected_revision != ledger['revision']:
            raise LightGraphError('resolution_revision_conflict')
        next_revision = expected_revision + 1
        now = datetime.now(timezone.utc).isoformat()
        if decision_id:
            decision = next((d for d in ledger['decisions'] if d['decision_id'] == decision_id
                             and d['candidate_id'] in session.index.groups and set(d['members']) <= session.index.nodes.keys()), None)
            if decision is None:
                raise LightGraphError('identity_decision_not_found')
            if decision['status'] != 'active':
                raise LightGraphError('resolution_revision_conflict')
            check_review_scope(session.raw, session.visible, decision['members'], merge=False)
            decision.update(status='revoked', revoked_revision=next_revision, revoked_at=now)
        else:
            if candidate_id not in session.index.groups:
                raise LightGraphError('identity_candidate_not_found')
            expanded, _ = session.index.groups[candidate_id]
            chosen = sorted(members if members is not None else expanded)
            if (not 2 <= len(chosen) <= 256 or len(chosen) != len(set(chosen)) or not set(chosen).issubset(expanded)
                    or verdict not in ('same', 'different')):
                raise LightGraphError('invalid_resolution_members')
            if verdict == 'same' and session.index.state.expanded(chosen) != set(chosen):
                raise LightGraphError('resolution_cluster_review_required')
            check_review_scope(session.raw, session.visible, chosen, merge=verdict == 'same')
            # A contrary verdict is tested against all prior decisions during replay.
            if session.index.state.resolved(chosen):
                raise LightGraphError('resolution_decision_conflict')
            decision = {'decision_id': 'pd_' + uuid4().hex, 'candidate_id': candidate_id,
                        'members': chosen, 'origin': 'user', 'status': 'active',
                        'verdict': verdict, 'revision': next_revision, 'created_at': now}
            ledger['decisions'].append(decision)
        result = project(session.raw, ledger['decisions'], session.base_id)
        result.update(storage_layout='parquet_v1', access_fingerprint=session.base['access_fingerprint'])
        ledger['revision'] = next_revision
        publication = 'published/' + uuid4().hex + '.graph'
        with _pending_review_graph(session, publication, result) as path:
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
