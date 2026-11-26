"""Ordinary Chinese material reaches fusion through production tools and committed artifacts.

Drafts and proposal selections are scripted, not a live-model quality claim.
No test manufactures a provenance authority marker or grants equal fingerprints.
"""
from copy import deepcopy

import pytest

from lightgraph.construction.access import filter_graph_scopes
from lightgraph.construction.checkpoints import commit_checkpoint, load_checkpoint
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.construction.overlap_patterns import analyze_overlap_patterns
from lightgraph.construction.projection_proposals import attach_checkpoint_grant, verify_projection_proposals
from lightgraph.construction.snapshot_registry import register_access_context
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workflow import load_unit_contributor
from lightgraph.construction.workspace import dump_yaml
from lightgraph.runtime.config import MergePolicyConfig, RunConfig


CLAIM = '字段 asset_code 对应设备编号。集团设备标识由各系统共享同一编号体系，在全集团唯一，相同编码代表同一设备。'


def published_branch(root, owner, *, quote=CLAIM, text=CLAIM, explain=True, domain='集团设备标识',
                     scope=None, value='001', name='主泵', property_name=None, extra='', isolation='enterprise', version=None,
                     identity_scope='global'):
    directory = root / owner
    directory.mkdir()
    source = directory / 'data.csv'
    prop = property_name or ('功率' if owner == 'left' else '维护商')
    source.write_text(f'asset_code,name,provider,detail,audience\n{value},{name},{owner}服务方,{owner}独有属性,{scope or owner}\n',
                      encoding='utf-8')
    document = directory / 'fields.md'
    document.write_text('---\nid: ' + owner + '\npurpose: data_description\nscope:\n'
        f'  tables: [{owner}.public.items]\n  columns: [asset_code]\n---\n# 字段解释\n{text}\n' + extra,
        encoding='utf-8')
    config = RunConfig(sources=[{'id': owner, 'kind': 'csv_file', 'path': str(source), 'table_name': 'items',
        'encoding': 'utf-8', 'delimiter': ',', 'header': True, 'null_values': [''], 'type_inference': 'text',
        'allowed_tables': ['public.items'], 'allowed_columns': {'public.items': ['asset_code', 'name', 'provider', 'detail', 'audience']}}],
        documents=[str(document)], isolation_domain=isolation, model={'mode': 'in_environment'},
        scope_bindings=[{'source_id': owner, 'table': 'items', 'columns': ['audience']}], scopes=[scope or owner])
    tools = ConstructionTools(config, directory / 'construction')
    try:
        material = tools.call('read_material', name='document:' + owner)
        assert material['section_fully_read'], material
        refs = material['evidence_refs']
        assert len(refs) == 1
        binding = {'source_id': owner, 'table': 'items'}
        kind = '设备' if owner == 'left' else '资产_' + owner
        explanation = {'source_id': owner, 'table': 'items', 'key_columns': ['asset_code'],
                       'key_roles': ['设备编号'], 'quote': quote, 'evidence_ref': refs[0], 'version': version}
        draft = {'draft_id': 'objects', 'entities': [
            {'type': kind, 'key_names': ['code'], 'identity_scope': identity_scope, 'identity_domain': domain,
             'identity_evidence_refs': refs, 'identity_explanations': [explanation] if explain else [],
             'mappings': [{**binding, 'key_columns': ['asset_code'], 'label_column': 'name',
                           'properties': {prop: 'detail'}, 'business_context': {'status': 'global'}}]},
            {'type': '服务方', 'key_names': ['name'], 'mappings': [{**binding, 'key_columns': ['provider'],
                            'label_column': 'provider', 'business_context': {'status': 'global'}}]}],
            'relations': [{'type': '服务于', 'source_type': kind, 'target_type': '服务方',
                          'mappings': [{**binding, 'source_key_columns': ['asset_code'], 'target_key_columns': ['provider'],
                                        'business_context': {'status': 'global'}}]}]}
        saved = tools.call('save_graph_draft', yaml_text=dump_yaml(draft))
        if saved['status'] != 'ok':
            return config, None, saved
        assert saved['identity_evidence_state']['interpreted_key_claims'] == int(explain)
        result = tools.call('validate_graph_draft', draft_id='objects')
        assert result['passed'], result
        published = tools.call('publish_graph_draft', draft_id='objects')
        assert published['status'] == 'ok', published
    finally:
        tools.close()
    contributor = load_unit_contributor(directory, owner)
    policy = register_access_context(directory / 'construction', config)
    record = commit_checkpoint(directory, 'run_' + 'a' * 24, owner, contributor, input_digest='fixed-input',
                               graph_kind='node_delta', result_policy_id=policy)
    loaded = load_checkpoint(directory, record)
    loaded['source_namespace'] = isolation
    return config, attach_checkpoint_grant(loaded, directory, record), record


def compile_proposal(branches):
    patterns = analyze_overlap_patterns(branches).patterns
    card, = [p for p in patterns if p['kind'] == 'entity' and p['basis'] == 'same_logical_keys_across_sources'
             and all(m['type'] != '服务方' for m in p['members'])]
    decision = {'action': 'propose', 'pattern_id': card['pattern_id'], 'alignment': [
        {'member_index': i, 'source_index': member['source_indexes'][0],
         'role_index': next(j for j, role in enumerate(member['roles']) if role['end'] == 'source')}
        for i, member in enumerate(card['members'])]}
    policy, reports = verify_projection_proposals(MergePolicyConfig(), branches, [card], [decision])
    return policy, reports[0]


def publish_combined_drafts(root):
    from lightgraph.construction.workspace import GraphWorkspace, read_internal_yaml

    configs = []
    for owner in ('left', 'right'):
        config, _, _ = published_branch(root, owner)
        configs.append(config)
    combined = configs[0].model_copy(update={
        'sources': [s for c in configs for s in c.sources],
        'documents': [d for c in configs for d in c.documents],
        'scope_bindings': [b for c in configs for b in c.scope_bindings], 'scopes': ['left', 'right']})
    directory = root / 'combined'
    tools = ConstructionTools(combined, directory / 'construction')
    try:
        for owner in ('left', 'right'):
            tools.call('read_material', name='document:' + owner)
            origin = root / owner / 'construction'
            head = read_internal_yaml(origin / 'manifest.yaml')['current_graph']
            old = GraphWorkspace(origin, head['context_fingerprint']).get_draft('objects').draft
            draft = old.model_copy(update={'draft_id': owner})
            saved = tools.call('save_graph_draft', yaml_text=dump_yaml(draft.model_dump(mode='json', by_alias=True)))
            assert saved['status'] == 'ok', saved
            validated = tools.call('validate_graph_draft', draft_id=owner)
            assert validated['passed'], validated
            assert tools.call('publish_graph_draft', draft_id=owner)['status'] == 'ok'
    finally:
        tools.close()
    contributor = load_unit_contributor(directory, 'combined')
    return combined, directory, contributor


@pytest.mark.parametrize('human_different', [False, True])
def test_two_drafts_in_one_worker_and_explicit_cannot_link(tmp_path, human_different):
    from lightgraph.construction.human_identity import check_review_scope, project
    from lightgraph.construction.workflow import publish_premerged_graph

    config, directory, contributor = publish_combined_drafts(tmp_path)
    if human_different:
        raw = contributor['graph']
        members = [n['id'] for n in raw['nodes'] if n['identity'].get('code') == '001']
        check_review_scope(raw, filter_graph_scopes(raw, ['left', 'right']), members, merge=False)
        reviewed = project(raw, [{'decision_id': 'explicit-difference', 'members': members,
                                 'verdict': 'different', 'status': 'active'}], 'original-publication')
        publish_premerged_graph(directory, config, merged=reviewed, included_units=['combined'],
                                skipped_units=[], unit_attempts={})
        contributor = load_unit_contributor(directory, 'combined')
    policy_id = register_access_context(directory / 'construction', config)
    record = commit_checkpoint(directory, 'run_' + 'b' * 24, 'combined', contributor, input_digest='two-drafts',
                               graph_kind='node_delta', result_policy_id=policy_id)
    loaded = load_checkpoint(directory, record)
    loaded['source_namespace'] = config.isolation_domain
    granted = attach_checkpoint_grant(loaded, directory, record)
    _, auxiliary, _ = published_branch(tmp_path, 'auxiliary', value='002', name='另一设备')
    policy, report = compile_proposal([granted, auxiliary])
    graph = merge_construction_graphs([granted, auxiliary], merge_policy=policy)
    objects = [n for n in graph['nodes'] if n['identity'].get('code') == '001']
    assert len(objects) == (2 if human_different else 1), report
    assert len(graph['edges']) == 3


def test_branch_agent_can_revise_local_type_alias_using_the_same_quoted_identity(tmp_path):
    config, directory, _ = publish_combined_drafts(tmp_path)
    tools = ConstructionTools(config, directory / 'construction')
    try:
        overlap = tools.call('inspect_branch_overlap')
        assert overlap['pattern_count'] > 0
        prior = tools.workspace.get_draft('right').draft.model_dump(mode='json', by_alias=True)
        prior['entities'][0]['type'] = '设备'
        prior['relations'][0]['source_type'] = '设备'
        result = tools.call('save_graph_draft', yaml_text=dump_yaml(prior))
        assert result['status'] == 'ok', result
        assert tools.call('validate_graph_draft', draft_id='right')['passed']
        result = tools.call('publish_graph_draft', draft_id='right')
        assert result['status'] == 'ok', result
        graph = tools.workspace.read_published_graph()
        objects = [n for n in graph['nodes'] if n['identity'].get('code') == '001']
        assert len(objects) == 1
        assert set(objects[0]['properties']) == {'功率', '维护商'}
        assert len(graph['edges']) == 2
    finally:
        tools.close()


def test_independent_chinese_materials_and_different_grants_merge_without_cross_scope_disclosure(tmp_path):
    left_config, left, _ = published_branch(tmp_path, 'left')
    right_config, right, _ = published_branch(tmp_path, 'right')
    branches = [left, right]
    assert left['graph']['access_fingerprint'] != right['graph']['access_fingerprint']
    policy, report = compile_proposal(branches)
    assert report['verified_groups'] == 1, report
    assert report['access_basis'] == 'individual_frozen_observation_grants'
    assert report['verified_groups_by_basis'] == {'shared_business_key_explanation': 1}
    graph = merge_construction_graphs(branches, merge_policy=policy)
    merged, = [n for n in graph['nodes'] if n['identity'].get('code') == '001']
    assert set(merged['properties']) == {'功率', '维护商'}
    assert len(graph['edges']) == 2
    from lightgraph.construction.workflow import publish_premerged_graph
    combined = left_config.model_copy(update={
        'sources': left_config.sources + right_config.sources,
        'documents': left_config.documents + right_config.documents,
        'scope_bindings': left_config.scope_bindings + right_config.scope_bindings,
        'scopes': ['left', 'right']})
    destination = tmp_path / 'fused'
    publish_premerged_graph(destination, combined, merged=graph, included_units=['left', 'right'],
                            skipped_units=[], unit_attempts={})
    graph = load_unit_contributor(destination, 'fused')['graph']
    from lightgraph.retrieval import SubgraphRetriever, sync_graph_to_retrieval
    sync_graph_to_retrieval(graph, tmp_path / 'retrieval')
    retriever = SubgraphRetriever(tmp_path / 'retrieval')
    for owner, forbidden in [('left', 'right'), ('right', 'left')]:
        visible = filter_graph_scopes(graph, [owner])
        node, = [n for n in visible['nodes'] if n['identity'].get('code') == '001']
        assert node['id'] == merged['id']
        assert len(node['supports']) == len(visible['edges']) == 1
        assert forbidden + '独有属性' not in str(visible)
        assert forbidden + '服务方' not in str(visible)
        assert all('identity_explanations' not in d for d in visible['definitions']['entities'])
        claim, = node['supports'][0]['provenance']['global_identity_authority']['interpreted_key_claims_v1']
        assert claim['quote'] == CLAIM and claim['document_id'] == owner
        recalled = retriever.retrieve(node_id=merged['id'], hops=1, scopes=[owner])
        assert len(recalled['edges']) == 1
        assert forbidden + '独有属性' not in str(recalled)
        assert forbidden + '服务方' not in str(recalled)


@pytest.mark.parametrize('fault', ['missing_interpretation', 'different_namespace', 'no_grant', 'trimmed_column', 'source_revoked'])
def test_insufficient_identity_or_grants_do_not_become_merge_authority(tmp_path, fault):
    _, left, _ = published_branch(tmp_path, 'left')
    _, right, _ = published_branch(tmp_path, 'right', explain=fault != 'missing_interpretation',
                                   isolation='other' if fault == 'different_namespace' else 'enterprise')
    if fault == 'no_grant':
        right.pop('_projection_grant')
    elif fault in ('trimmed_column', 'source_revoked'):
        grant = right['_projection_grant']
        # Simulate a narrowed runtime grant; the original graph and its fingerprint stay unchanged.
        policy = deepcopy(grant.policy)
        if fault == 'trimmed_column':
            policy['sources'][0]['allowed_columns']['public.items'].remove('detail')
        else:
            policy['sources'][0]['allow_fact_data'] = False
        from lightgraph.construction.projection_access import _CheckpointGrant
        right['_projection_grant'] = _CheckpointGrant(grant.fingerprint, policy)
    policy, report = compile_proposal([left, right])
    assert report['verified_groups'] == 0, report
    graph = merge_construction_graphs([left, right], merge_policy=policy)
    assert len([n for n in graph['nodes'] if n['identity'].get('code') == '001']) == 2


@pytest.mark.parametrize('conflict', [False, True])
def test_three_independent_branches_need_one_common_identity_version(tmp_path, conflict):
    branches = []
    for owner in ('left', 'middle', 'right'):
        version = 'v2' if conflict and owner == 'right' else 'v1'
        claim = CLAIM + f'标识体系版本为 {version}。'
        _, branch, _ = published_branch(tmp_path, owner, quote=claim, text=claim, version=version)
        branches.append(branch)
    policy, report = compile_proposal(branches)
    graph = merge_construction_graphs(branches, merge_policy=policy)
    objects = [n for n in graph['nodes'] if n['identity'].get('code') == '001']
    assert len(objects) == (3 if conflict else 1), report
    if not conflict:
        assert len(objects[0]['supports']) == len(graph['edges']) == 3
    else:
        assert report['verified_groups'] == 0


@pytest.mark.parametrize(('quote', 'text', 'extra', 'code'), [
    (CLAIM + '伪造文字', CLAIM, '', 'identity_explanation_quote_mismatch'),
    (CLAIM, CLAIM, '# 独立编号\nasset_code 由各站独立编号，同码不代表同一设备。\n', 'identity_material_conflict'),
    ('字段 asset_code 对应设备编号，集团设备标识用于查询。',
     '字段 asset_code 对应设备编号，集团设备标识用于查询。', '', 'identity_explanation_requires_semantic_review'),
    (CLAIM + '但是仅在本工厂使用。', CLAIM + '但是仅在本工厂使用。', '',
     'identity_explanation_requires_semantic_review'),
    (CLAIM, CLAIM, '# 编码限制\nasset_code 不具备全局唯一性。\n', 'identity_material_conflict'),
    (CLAIM.replace('asset_code', 'old_asset_code'), CLAIM.replace('asset_code', 'old_asset_code'), '',
     'identity_explanation_key_roles_unverified'),
])
def test_exact_quote_and_uncited_contrary_material_are_checked(tmp_path, quote, text, extra, code):
    _, branch, response = published_branch(tmp_path, 'left', quote=quote, text=text, extra=extra)
    assert branch is None
    assert code in str(response), response


@pytest.mark.parametrize('statement', [
    '集团设备标识不保证跨系统唯一。', '集团设备标识不能确保全局唯一。',
    '集团设备标识尚未确认全局唯一。', '集团设备标识的目标是全局唯一。',
    '集团设备标识要求全局唯一。', '例如集团设备标识在全集团唯一。',
    '集团设备标识用于记录。另一个目录全局唯一。',
    '集团设备标识是否全局唯一尚不清楚。',
    '集团设备标识全局唯一。',
    '集团设备标识是本系统设备的全局唯一编号。',
])
def test_non_assertions_never_establish_shared_identity(tmp_path, statement):
    text = '字段 asset_code 对应设备编号。' + statement
    _, branch, response = published_branch(tmp_path, 'left', quote=text, text=text)
    assert branch is None
    assert response['code'] in {'identity_material_conflict', 'identity_explanation_requires_semantic_review'}
