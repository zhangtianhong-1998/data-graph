"""Immutable YAML graph versions and backend-owned validation receipts."""
from copy import deepcopy
import pytest
from pydantic import ValidationError
from lightgraph.construction.models import GraphDraft
from lightgraph.construction.workspace import GraphWorkspace, dump_yaml, safe_load_yaml
from lightgraph.errors import LightGraphError


def payload(draft_id='物品关系', kind='物品'):
    return {'draft_id': draft_id, 'entities': [{'type': kind, 'key_names': ['编号'],
            'mappings': [{'source_id': 's', 'table': '任意', 'key_columns': ['甲'], 'label_column': '乙'}]}]}


def graph(node_id='n1', kind='物品', label='名称'):
    return {'format_version': 1, 'target': 'graph', 'status': 'validated', 'semantic_origin': 'agent_authored',
            'definitions': {'entities': [{'type': kind, 'description': '', 'key_names': ['编号']}], 'relations': []},
            'nodes': [{'id': node_id, 'type': kind, 'label': label, 'identity': {'编号': node_id},
                       'aliases': [], 'properties': {}, 'provenance': []}], 'edges': [], 'evidence_refs': []}


def test_yaml_preserves_codes_without_implicit_dates_or_booleans():
    value = safe_load_yaml("code: 0012\nword: yes\ndate: 2026-11-03\nflag: true\ncount: 12\n")
    assert value == {'code': '0012', 'word': 'yes', 'date': '2026-11-03', 'flag': True, 'count': 12}
    assert safe_load_yaml(dump_yaml(value)) == value


@pytest.mark.parametrize('text', ["status: candidate\nstatus: published", "x: !!python/object/apply:os.system ['echo unexpected']",
    'x: .nan', 'x: .inf', 'x: !!float .nan', 'x: !!timestamp 2026-11-03',
    'x: !!binary YWJj', 'x: &a [1]\ny: *a', 'x: 1\n---\nx: 2'])
def test_unsafe_yaml_is_rejected(text):
    with pytest.raises(LightGraphError):
        safe_load_yaml(text)


def test_no_legacy_rule_or_self_approval_fields():
    for field in ('rules', 'status', 'validation', 'semantic_status'):
        with pytest.raises(ValidationError):
            GraphDraft.model_validate({**payload(), field: []})
    draft = payload()
    draft['entities'][0]['mappings'][0]['predicate'] = {'x': float('nan')}
    with pytest.raises(ValidationError):
        GraphDraft.model_validate(draft)


def test_revisions_retain_history_and_invalidate_checks(tmp_path):
    store = GraphWorkspace(tmp_path, 'ctx')
    original = payload()
    first = store.save_draft(original)
    store.record_validation(first.draft.draft_id, revision=1, passed=True, graph=graph())
    assert store.save_draft(original).status == 'validated'
    retained = {p: p.read_bytes() for p in (tmp_path / 'drafts').glob('*.yaml')}
    original['entities'][0]['mappings'][0]['label_column'] = '丙'
    revised = store.save_draft(original)
    assert revised.revision == 2 and revised.validation is None and revised.status == 'candidate'
    assert all(p.read_bytes() == text for p, text in retained.items())
    with pytest.raises(LightGraphError, match='graph_draft_revision_conflict'):
        store.record_validation(revised.draft.draft_id, revision=1, passed=True)
    failed = store.record_validation(revised.draft.draft_id, revision=2, passed=False,
                                    diagnostics=[{'code': 'missing_column'}])
    assert failed.status == 'needs_revision'
    assert GraphWorkspace(tmp_path, 'ctx').get_draft(revised.draft.draft_id) == failed


def test_evidence_short_refs_and_refresh(tmp_path):
    store = GraphWorkspace(tmp_path, 'ctx')
    assert store.issue_evidence('schema', details={'count': 2}).ref == 'e1'
    assert store.issue_evidence('profile').ref == 'e2'
    candidate = payload()
    candidate['evidence_refs'] = ['invented']
    with pytest.raises(LightGraphError, match='unknown_construction_evidence'):
        store.save_draft(candidate)
    candidate['evidence_refs'] = ['e1']
    store.save_draft(candidate)
    with pytest.raises(LightGraphError, match='evidence_authority_field_forbidden'):
        store.issue_evidence('profile', details={'validated': True})
    with pytest.raises(LightGraphError, match='evidence_reference_conflict'):
        store.issue_evidence('profile', ref='e1')
    changed = GraphWorkspace(tmp_path, 'changed')
    assert changed.get_draft(candidate['draft_id']).status == 'stale'
    with pytest.raises(LightGraphError, match='stale_graph_draft'):
        changed.record_validation(candidate['draft_id'], revision=1, passed=True)
    assert changed.read_summary()['evidence_refs'] == []
    changed.issue_evidence('schema', ref='e1', details={'count': 3})
    revised = changed.save_draft(candidate)
    assert revised.revision == 2 and revised.status == 'candidate'


def test_publish_requires_exact_graph_revision_and_preserves_independent_outputs(tmp_path):
    store = GraphWorkspace(tmp_path, 'ctx')
    store.save_draft(payload())
    with pytest.raises(LightGraphError, match='graph_validation_required'):
        store.publish('物品关系', revision=1, graph=graph())
    store.record_validation('物品关系', revision=1, passed=True, graph=graph())
    changed = graph(label='篡改')
    with pytest.raises(LightGraphError, match='validated_graph_mismatch'):
        store.publish('物品关系', revision=1, graph=changed)
    first = store.publish('物品关系', revision=1, graph=graph())
    first_bytes = (tmp_path / first['graph']).read_bytes()
    other = payload('另一个', '位置')
    store.save_draft(other)
    second_graph = graph('n2', '位置')
    store.record_validation('另一个', revision=1, passed=True, graph=second_graph)
    store.publish('另一个', revision=1, graph=second_graph)
    assert len(store.read_published_graph()['nodes']) == 2
    assert (tmp_path / first['graph']).read_bytes() == first_bytes
    conflicting = payload('冲突')
    store.save_draft(conflicting)
    store.record_validation('冲突', revision=1, passed=True, graph=changed)
    with pytest.raises(LightGraphError, match='published_graph_identity_conflict'):
        store.publish('冲突', revision=1, graph=changed)
    assert len(store.read_published_graph()['nodes']) == 2
    with pytest.raises(LightGraphError, match='stale_published_graph'):
        GraphWorkspace(tmp_path, 'new').read_published_graph()


def test_manifest_cannot_point_outside_workspace(tmp_path):
    store = GraphWorkspace(tmp_path / 'work', 'ctx')
    store.save_draft(payload())
    manifest = safe_load_yaml(store.manifest_path.read_text())
    manifest['drafts']['物品关系']['current'] = '../secret.yaml'
    store.manifest_path.write_text(dump_yaml(manifest))
    with pytest.raises(LightGraphError, match='invalid_graph_workspace_record'):
        store.get_draft('物品关系')


def test_publications_with_different_access_cannot_merge_or_replace_head(tmp_path):
    store = GraphWorkspace(tmp_path, 'ctx')
    first = {**graph(), 'access_fingerprint': 'scope_a'}
    store.save_draft(payload('first'))
    store.record_validation('first', revision=1, passed=True, graph=first)
    store.publish('first', revision=1, graph=first)
    manifest_before = store.manifest_path.read_bytes()
    second = {**graph('n2'), 'access_fingerprint': 'scope_b'}
    store.save_draft(payload('second'))
    store.record_validation('second', revision=1, passed=True, graph=second)
    head = safe_load_yaml(manifest_before.decode())['current_graph']
    with pytest.raises(LightGraphError, match='published_graph_access_conflict'):
        store.publish('second', revision=1, graph=second)
    assert safe_load_yaml(store.manifest_path.read_text())['current_graph'] == head
    assert store.read_published_graph()['access_fingerprint'] == 'scope_a'
