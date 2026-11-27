"""Regression tests for semantic merge, overlap review, Chinese explanations, prefix fusion, retrieval, and Web/UI fixes."""
from copy import deepcopy
import json
import threading

import pytest

from lightgraph.construction.fusion import normalize_key_value
from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.construction.overlap_patterns import analyze_overlap_patterns
from lightgraph.construction.overlap_review import review_overlap_patterns
from lightgraph.construction.projection_proposals import verify_projection_proposals
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import dump_yaml, write_yaml
from lightgraph.contracts import digest
from lightgraph.documents.identity_contract import shared_key_claim_refs
from lightgraph.documents.identity_explanations import explanation_claims
from lightgraph.errors import LightGraphError
from lightgraph.retrieval.agent_context import agent_context
from lightgraph.retrieval.retriever import SubgraphRetriever
from lightgraph.retrieval.sync import sync_graph_to_retrieval
from lightgraph.runtime.config import EntityFusionRule, MergePolicyConfig, RunConfig
from lightgraph.sources import MemorySource
from lightgraph.tests.test_batch_overlap_patterns import unit
from lightgraph.tests.test_chinese_identity_production import compile_proposal, published_branch
from lightgraph.tests.test_overlap_pattern_model import alignment_card, configured, profile, respond, setup_provider
from lightgraph.tests.test_projection_proposals import _bind_section, _compile
from lightgraph.web import graph_history
from lightgraph.web.execution import RunManager
from lightgraph.web.workspace import Workspace


def _composite_key_unit(owner, entity_type, field, *, single_key=False, key_names=None):
    """Build a unit where label_column is part of a composite (code, name) key (or sole key)."""
    keys = ['item_name'] if single_key else ['item_code', 'item_name']
    key_names = key_names or (['name'] if single_key else [f'{owner}_code', f'{owner}_name'])
    identity = {key_names[0]: '净销售收入'} if single_key else {key_names[0]: 'PL_91000', key_names[1]: '净销售收入'}
    id_types = {k: 'text' for k in key_names}
    row_values = {'item_name': '净销售收入', 'group_code': 'G1'} if single_key else {
        'item_code': 'PL_91000', 'item_name': '净销售收入', 'group_code': 'G1'}
    row_types = {k: 'text' for k in row_values}
    src = {'source_id': 'shared_xlsx', 'schema': 'public', 'table': 'Sheet1'}
    nid = f'{owner}_item'
    gid = f'{owner}_group'
    graph = {
        'format_version': 1, 'target': 'graph', 'status': 'published',
        'support_visibility_version': 1, 'access_fingerprint': 'fp_shared',
        'definitions': {
            'entities': [
                {'type': entity_type, 'key_names': key_names, 'identity_origin': 'row',
                 'identity_scope': 'global'},
                {'type': f'{owner}_Group', 'key_names': ['group_code'], 'identity_origin': 'row',
                 'identity_scope': 'global'},
            ],
            'relations': [{
                'type': 'belongs_to_group', 'source_type': entity_type, 'target_type': f'{owner}_Group',
                'hierarchy_orientation': 'none',
            }],
        },
        'nodes': [
            {
                'id': nid, 'type': entity_type, 'identity': identity, 'identity_types': id_types,
                'label': '净销售收入', 'properties': {field: [f'{owner}_val']},
                'supports': [{
                    'scope_ref': 'public', 'contributor_id': owner, 'label': '净销售收入',
                    'observed_label': '净销售收入', 'properties': {field: [f'{owner}_val']},
                    'business_context': {'status': 'global', 'dimensions': {}},
                    'row_context': {'id': 'rc_shared', 'values': row_values, 'value_types': row_types},
                    'source_row_witness': {'snapshot': 'snap_1', 'rows': [1]},
                    'provenance': {
                        'definition': entity_type, 'mapping': 0, 'source': src,
                        'key_columns': keys, 'label_column': 'item_name',
                        'property_columns': {field: f'{field}_col'},
                        'evidence_refs': ['ev1'],
                    },
                }],
            },
            {
                'id': gid, 'type': f'{owner}_Group', 'identity': {'group_code': 'G1'},
                'identity_types': {'group_code': 'text'}, 'label': '利润表组', 'properties': {},
                'supports': [{
                    'scope_ref': 'public', 'contributor_id': owner, 'label': '利润表组',
                    'observed_label': '利润表组', 'properties': {},
                    'business_context': {'status': 'global', 'dimensions': {}},
                    'row_context': {'id': 'rc_shared', 'values': row_values, 'value_types': row_types},
                    'source_row_witness': {'snapshot': 'snap_1', 'rows': [1]},
                    'provenance': {
                        'definition': f'{owner}_Group', 'mapping': 0, 'source': src,
                        'key_columns': ['group_code'], 'label_column': 'group_code',
                        'evidence_refs': ['ev1'],
                    },
                }],
            },
        ],
        'edges': [{
            'id': f'{owner}_edge', 'type': 'belongs_to_group', 'source': nid, 'target': gid,
            'supports': [{
                'scope_ref': 'public', 'contributor_id': owner,
                'business_context': {'status': 'global', 'dimensions': {}},
                'source_row_witness': {'snapshot': 'snap_1', 'rows': [1]},
                'provenance': {
                    'definition': 'belongs_to_group', 'mapping': 0, 'source': src,
                    'source_key_columns': keys, 'target_key_columns': ['group_code'],
                    'evidence_refs': ['ev1'],
                },
            }],
        }],
        'evidence_refs': ['ev1'],
    }
    return {'contributor_id': owner, 'kind': 'unit', 'source_namespace': 'workspace1', 'graph': graph}


def test_composite_code_name_key_verifies_in_projection_proposals_and_overlap_patterns():
    inputs = [_composite_key_unit('u1', 'ReportItemA', 'metric_a'),
              _composite_key_unit('u2', 'ReportItemB', 'metric_b')]
    analysis = analyze_overlap_patterns(inputs)
    card, = [p for p in analysis.patterns if p['kind'] == 'entity' and p['basis'] == 'same_physical_key'
             and any('ReportItem' in m['type'] for m in p['members'])]
    rows = card['binding_comparisons']['rows']
    assert rows[0]['matching_observed_names'] == 1
    assert rows[0]['unverified_names'] == 0

    alignment = [{'member_index': i, 'source_index': m['source_indexes'][0],
                  'role_index': next(j for j, r in enumerate(m['roles']) if r['end'] == 'source')}
                 for i, m in enumerate(card['members'])]
    policy, (report,) = verify_projection_proposals(
        MergePolicyConfig(), inputs, [card],
        [{'action': 'propose', 'pattern_id': card['pattern_id'], 'alignment': alignment}])
    assert report['verified_groups'] == 1, report
    assert report['verified_groups_by_basis'] == {'same_physical_projection': 1}
    merged = merge_construction_graphs(inputs, merge_policy=policy)
    assert merged['fusion_diagnostics']['model_verified_projections']['collapsed_nodes'] == 1
    merged_item, = [n for n in merged['nodes'] if len(n['supports']) == 2]
    assert set(merged_item['properties']) == {'metric_a', 'metric_b'}

    # Negative guard: single-key where label_column IS the sole key column must still fail.
    single_inputs = [_composite_key_unit('u1', 'NameOnlyA', 'metric_a', single_key=True),
                     _composite_key_unit('u2', 'NameOnlyB', 'metric_b', single_key=True)]
    single_analysis = analyze_overlap_patterns(single_inputs)
    single_card, = [p for p in single_analysis.patterns if p['kind'] == 'entity' and p['basis'] == 'same_physical_key'
                    and any('NameOnly' in m['type'] for m in p['members'])]
    single_alignment = [{'member_index': i, 'source_index': m['source_indexes'][0],
                         'role_index': next(j for j, r in enumerate(m['roles']) if r['end'] == 'source')}
                        for i, m in enumerate(single_card['members'])]
    _, (single_report,) = verify_projection_proposals(
        MergePolicyConfig(), single_inputs, [single_card],
        [{'action': 'propose', 'pattern_id': single_card['pattern_id'], 'alignment': single_alignment}])
    assert single_report['verified_groups'] == 0
    assert single_report['rejected_keys_by_reason'] == {'unverified_name_or_other_bindings': 1}


def _cross_table_hierarchy_unit(owner, table, grandparent_code):
    src = {'source_id': 'warehouse', 'schema': 'public', 'table': table}
    domain = 'enterprise_report_item'
    section = digest('identity_field_section_v1', 'report-dict', 'v1', 'items', 'content')
    nodes = []
    for idx, (code, name, col) in enumerate([
        (grandparent_code, '根类', 'l1_code'),
        ('P1', '父类', 'l2_code'),
        ('C1', '子类', 'l3_code'),
    ]):
        address = (src['source_id'], src['schema'], src['table'])
        nodes.append({
            'id': f'{owner}_{idx}', 'type': f'{owner}_Item',
            'identity': {'code': code}, 'identity_types': {'code': 'text'},
            'identity_domain': domain, 'label': name, 'properties': {f'p_{owner}': [f'v_{idx}']},
            'supports': [{
                'scope_ref': 'public', 'contributor_id': owner, 'label': name, 'observed_label': name,
                'properties': {f'p_{owner}': [f'v_{idx}']},
                'business_context': {'status': 'global', 'dimensions': {}},
                'source_row_witness': {'snapshot': f'snap_{owner}', 'rows': [idx + 1]},
                'provenance': {
                    'definition': f'{owner}_Item', 'mapping': idx, 'source': src,
                    'key_columns': [col], 'label_column': f'{col}_name',
                    'property_columns': {f'p_{owner}': f'p_{owner}_col'},
                    'evidence_refs': ['ev1'],
                    'global_identity_authority': {
                        'binding': digest(domain, address, [col]),
                        'key_sections_v1': [section], 'shared_key_sections_v2': [section],
                    },
                },
            }],
        })
    edges = [
        {
            'id': f'{owner}_e1', 'type': 'has_child', 'source': f'{owner}_0', 'target': f'{owner}_1',
            'supports': [{
                'scope_ref': 'public', 'contributor_id': owner,
                'business_context': {'status': 'global', 'dimensions': {}},
                'source_row_witness': {'snapshot': f'snap_{owner}', 'rows': [1]},
                'provenance': {
                    'definition': 'has_child', 'mapping': 0, 'source': src,
                    'source_key_columns': ['l1_code'], 'target_key_columns': ['l2_code'],
                    'evidence_refs': ['ev1'],
                },
            }],
        },
        {
            'id': f'{owner}_e2', 'type': 'has_child', 'source': f'{owner}_1', 'target': f'{owner}_2',
            'supports': [{
                'scope_ref': 'public', 'contributor_id': owner,
                'business_context': {'status': 'global', 'dimensions': {}},
                'source_row_witness': {'snapshot': f'snap_{owner}', 'rows': [2]},
                'provenance': {
                    'definition': 'has_child', 'mapping': 1, 'source': src,
                    'source_key_columns': ['l2_code'], 'target_key_columns': ['l3_code'],
                    'evidence_refs': ['ev1'],
                },
            }],
        },
    ]
    graph = {
        'format_version': 1, 'target': 'graph', 'status': 'published',
        'support_visibility_version': 1, 'access_fingerprint': 'fp_same',
        'definitions': {
            'entities': [{'type': f'{owner}_Item', 'key_names': ['code'], 'identity_origin': 'row',
                          'identity_scope': 'global', 'identity_domain': domain}],
            'relations': [{'type': 'has_child', 'source_type': f'{owner}_Item', 'target_type': f'{owner}_Item',
                           'hierarchy_orientation': 'parent_to_child', 'acyclic': True}],
        },
        'nodes': nodes, 'edges': edges, 'evidence_refs': ['ev1'],
    }
    return {'contributor_id': owner, 'kind': 'unit', 'source_namespace': 'workspace1', 'graph': graph}


def test_cross_table_identical_hierarchy_merges_and_different_grandparent_is_rejected():
    same_ancestors = [_cross_table_hierarchy_unit('u1', 'table_a', 'GP_1'),
                      _cross_table_hierarchy_unit('u2', 'table_b', 'GP_1')]
    analysis = analyze_overlap_patterns(same_ancestors)
    card, = [p for p in analysis.patterns if p['kind'] == 'entity' and p['basis'] == 'same_logical_keys_across_sources']
    alignment = []
    for idx, member in enumerate(card['members']):
        role_idx = next(i for i, r in enumerate(member['roles'])
                        if r['end'] == 'target' and r['key_columns'] == ['l3_code'])
        src_idx = member['roles'][role_idx]['source_indexes'][0]
        alignment.append({'member_index': idx, 'source_index': src_idx, 'role_index': role_idx})
    policy, (report,) = verify_projection_proposals(
        MergePolicyConfig(), same_ancestors, [card],
        [{'action': 'propose', 'pattern_id': card['pattern_id'], 'alignment': alignment}])
    assert report['verified_groups'] == 1, report
    assert report['verified_groups_by_basis'] == {'shared_versioned_key_section': 1}
    merged = merge_construction_graphs(same_ancestors, merge_policy=policy)
    assert merged['fusion_diagnostics']['model_verified_projections']['collapsed_nodes'] == 1

    # Negative guard: different grandparent code must still be rejected as different_ancestor_context.
    diff_ancestors = [_cross_table_hierarchy_unit('u1', 'table_a', 'GP_1'),
                      _cross_table_hierarchy_unit('u2', 'table_b', 'GP_OTHER')]
    analysis_diff = analyze_overlap_patterns(diff_ancestors)
    card_diff, = [p for p in analysis_diff.patterns
                  if p['kind'] == 'entity' and p['basis'] == 'same_logical_keys_across_sources']
    alignment_diff = []
    for idx, member in enumerate(card_diff['members']):
        role_idx = next(i for i, r in enumerate(member['roles'])
                        if r['end'] == 'target' and r['key_columns'] == ['l3_code'])
        src_idx = member['roles'][role_idx]['source_indexes'][0]
        alignment_diff.append({'member_index': idx, 'source_index': src_idx, 'role_index': role_idx})
    _, (report_diff,) = verify_projection_proposals(
        MergePolicyConfig(), diff_ancestors, [card_diff],
        [{'action': 'propose', 'pattern_id': card_diff['pattern_id'], 'alignment': alignment_diff}])
    assert report_diff['verified_groups'] == 0
    assert report_diff['rejected_keys_by_reason'] == {'different_ancestor_context': 1}


def test_overlap_review_strips_json_schema_null_union_fields_and_gives_targeted_feedback(monkeypatch):
    card = alignment_card()
    other = profile(9, eligible=False)

    def reply(request):
        issued = json.loads(json.loads(request.content)['messages'][1]['content'])['patterns']
        decisions = []
        for p in issued:
            if p['pattern_id'] == card['pattern_id']:
                bids = [c['binding_id'] for c in p['binding_choices']]
                decisions.append({
                    'pattern_id': p['pattern_id'], 'action': 'propose',
                    'binding_ids': bids, 'alignment': None, 'member_indexes': [], 'member_ids': None,
                })
            else:
                decisions.append({
                    'pattern_id': p['pattern_id'], 'action': 'review',
                    'binding_ids': None, 'alignment': None, 'member_indexes': None, 'member_ids': None,
                })
        return respond(json.dumps({'decisions': decisions}))

    setup_provider(monkeypatch, reply)
    report = review_overlap_patterns(configured(), [card, other], api_key='key')
    assert report['status'] == 'reviewed' and report['reviewed_patterns'] == 2
    assert report['decisions'][0]['action'] == 'propose'
    assert report['decisions'][1] == {'pattern_id': other['pattern_id'], 'action': 'review'}

    # Verify targeted protocol_feedback on ineligible_decision retry.
    seen_feedback = []
    calls = 0

    def reply_retry(request):
        nonlocal calls
        calls += 1
        issued = json.loads(json.loads(request.content)['messages'][1]['content'])['patterns']
        if calls == 1:
            return respond(json.dumps({'decisions': [{'pattern_id': issued[0]['pattern_id'], 'action': 'merge'}]}))
        seen_feedback.append(issued[0]['protocol_feedback'])
        return respond(json.dumps({'decisions': [{'pattern_id': issued[0]['pattern_id'], 'action': 'review'}]}))

    setup_provider(monkeypatch, reply_retry)
    retried = review_overlap_patterns(configured(), [other], api_key='key')
    assert retried['status'] == 'reviewed' and retried['recovered_patterns'] == 1
    assert seen_feedback[0]['reason'] == 'ineligible_decision'
    assert 'review_actions' in seen_feedback[0]['instruction']


def test_chinese_identity_explanations_and_contract_scoping(tmp_path):
    # Affirmative "不同系统共享" + "全集团" + Markdown table row + role-before-column phrasing.
    for sub in ('md', 'role_first', 'en_neg'):
        (tmp_path / sub).mkdir()
    markdown_claim = (
        '| asset_code | 设备编号 |\n'
        '集团设备标识由不同系统共享同一编码体系，在全集团唯一，相同编码代表同一设备。'
    )
    branches = [
        published_branch(tmp_path / 'md', 'left', text=markdown_claim, quote=markdown_claim)[1],
        published_branch(tmp_path / 'md', 'right', text=markdown_claim, quote=markdown_claim)[1],
    ]
    policy, report = compile_proposal(branches)
    assert report['verified_groups'] == 1, report
    assert merge_construction_graphs(branches, merge_policy=policy)['fusion_diagnostics'][
        'model_verified_projections']['collapsed_nodes'] == 1

    role_first_claim = '设备编号对应字段 asset_code。集团设备标识在全集团统一编码，不同来源共享，相同编码代表同一设备。'
    _, left_branch, saved = published_branch(tmp_path / 'role_first', 'left', text=role_first_claim, quote=role_first_claim)
    assert left_branch is not None, saved

    # English sentence-period split still rejects "Not shared globally."
    english_rejected = (
        'Column asset_code identifies asset_id. Global Asset Registry is shared across systems. '
        'Not shared globally.'
    )
    _, bad_branch, saved_bad = published_branch(
        tmp_path / 'en_neg', 'left', domain='Global Asset Registry', text=english_rejected, quote=english_rejected)
    assert bad_branch is None and saved_bad['status'] == 'error'

    # Unrelated identity_contract column must not suppress prose restriction checks on another column.
    from lightgraph.contracts import ColumnSpec, TableSpec
    tbl = TableSpec(source_id='s', schema='public', name='orders',
                    columns=[ColumnSpec(name='global_id', data_type='varchar'),
                             ColumnSpec(name='local_seq', data_type='varchar')])
    applicable = [{
        'doc:s1': {
            'doc_id': 'doc', 'revision': '1', 'heading': 's1',
            'scope': {'columns': ['local_seq'], 'tables': ['s.public.orders']},
            'text': 'local_seq 仅在本表局部唯一，不得跨表合并。',
            'identity_bindings': [{
                'table': 's:public.orders', 'key_columns': ['global_id'],
                'scope': 'shared', 'domain': 'orders',
            }],
        },
        'doc:s2': {
            'doc_id': 'doc', 'revision': '1', 'heading': 's2',
            'scope': {'columns': ['local_seq'], 'tables': ['s.public.orders']},
            'text': 'orders 字段 local_seq 是全局统一编号。',
            'identity_bindings': [],
        },
    }]
    # Because doc:s1 has a prose restriction on local_seq and its identity_binding is for global_id (unrelated),
    # shared_key_claim_refs MUST NOT let doc:s1 be skipped as declared_sections and must return empty set!
    assert shared_key_claim_refs('orders', ['local_seq'], ['local_seq'], tbl, applicable, {'doc:s2'}) == set()

    # Inner-loop conflict must not be overwritten by a later clean section.
    quote = '字段 asset_code 对应设备编号。集团设备标识由各系统共享同一编码体系，在全集团唯一，相同编码代表同一设备。'
    exp = type('Exp', (), {
        'evidence_ref': 'doc:sec_clean', 'source_id': 's', 'schema_name': 'public', 'table': 'orders',
        'key_columns': ['asset_code'], 'key_roles': ['设备编号'], 'quote': quote, 'version': None,
    })()
    mapping = type('M', (), {'source_id': 's', 'schema_name': 'public', 'table': 'orders',
                             'key_columns': ['asset_code']})()
    ent_conflict = type('E2', (), {
        'type': 'Asset', 'key_names': ['code'], 'identity_scope': 'global', 'identity_domain': '集团设备标识',
        'identity_evidence_refs': ['doc:sec_clean'], 'identity_explanations': [exp], 'mappings': [mapping],
    })()
    tbl_asset = TableSpec(source_id='s', schema='public', name='orders',
                          columns=[ColumnSpec(name='asset_code', data_type='varchar')])
    sec_map = {
        'doc:sec_clean': {
            'doc_id': 'doc', 'revision': '1', 'heading': 'sec_clean',
            'scope': {'columns': ['asset_code'], 'tables': ['s.public.orders']},
            'text': quote, 'identity_bindings': [],
        },
        'doc:sec_conflict': {
            'doc_id': 'doc', 'revision': '1', 'heading': 'sec_conflict',
            'scope': {'columns': ['asset_code'], 'tables': ['s.public.orders']},
            'text': '字段说明。',
            'identity_bindings': [{'table': 's:public.orders', 'key_columns': ['asset_code'],
                                   'scope': 'local', 'domain': '集团设备标识'}],
        },
        'doc:sec_after': {
            'doc_id': 'doc', 'revision': '1', 'heading': 'sec_after',
            'scope': {'columns': ['asset_code'], 'tables': ['s.public.orders']},
            'text': '例如 asset_code 可能在全集团唯一。', 'identity_bindings': [],
        },
    }
    claims, issues = explanation_claims(ent_conflict, mapping, tbl_asset, [sec_map], {'doc:sec_clean'})
    assert any(d['code'] == 'identity_material_conflict' for d in issues), issues
    assert claims == []


def test_key_prefix_strips_fusion_and_retrieval_source_identity(tmp_path):
    # Longest prefix matches first and empty key_prefix_strips is omitted from serialized JSON.
    assert normalize_key_value('CCCBDA_PL_91750', [], ['CCC_', 'CCCBDA_']) == 'PL_91750'
    assert normalize_key_value('GEO_PL_91750', ['trim'], ['GEO_', 'INSTM_']) == 'PL_91750'
    plain_rule = EntityFusionRule(entity_type='Item', canonical_type='Item', identity_domain='fin',
                                  key_mapping={'code': 'code'})
    assert 'key_prefix_strips' not in plain_rule.model_dump(mode='json')
    with pytest.raises(LightGraphError, match='fusion_empty_normalized_value'):
        normalize_key_value('GEO_', [], ['GEO_'])

    def make_branch(owner, table_name, raw_code):
        src = {'source_id': 'dm_fin', 'schema': 'dm_fin', 'table': table_name}
        return {
            'contributor_id': owner, 'kind': 'unit',
            'graph': {
                'format_version': 1, 'target': 'graph', 'status': 'published',
                'support_visibility_version': 1,
                'definitions': {'entities': [{'type': f'ReportItem_{owner}', 'key_names': ['item_code']}], 'relations': []},
                'nodes': [{
                    'id': f'{owner}_n1', 'type': f'ReportItem_{owner}',
                    'identity': {'item_code': raw_code}, 'identity_types': {'item_code': 'text'},
                    'label': '拨备', 'properties': {},
                    'supports': [{
                        'scope_ref': 'public', 'label': '拨备', 'properties': {},
                        'business_context': {'status': 'global', 'dimensions': {}},
                        'provenance': {
                            'definition': f'ReportItem_{owner}', 'mapping': 0, 'source': src,
                            'key_columns': ['report_item_code'], 'label_column': 'report_item_cn_name',
                            'evidence_refs': ['e1'],
                        },
                    }],
                }],
                'edges': [], 'evidence_refs': ['e1'],
            },
        }

    inputs = [make_branch('geo', 'dm_fin_geo_pl', 'GEO_PL_91750'),
              make_branch('instm', 'dm_fin_instm_pl', 'INSTM_PL_91750')]
    policy = {
        'fusion_policy': {
            'entity_rules': [
                {'entity_type': 'ReportItem_geo', 'canonical_type': 'ReportItem', 'identity_domain': 'fin_pl',
                 'key_mapping': {'code': 'item_code'}, 'key_prefix_strips': {'code': ['GEO_', 'INSTM_']}},
                {'entity_type': 'ReportItem_instm', 'canonical_type': 'ReportItem', 'identity_domain': 'fin_pl',
                 'key_mapping': {'code': 'item_code'}, 'key_prefix_strips': {'code': ['GEO_', 'INSTM_']}},
            ],
        },
    }
    merged = merge_construction_graphs(inputs, merge_policy=policy)
    assert len(merged['nodes']) == 1
    node = merged['nodes'][0]
    assert node['identity'] == {'code': 'PL_91750'}
    assert {s['original_key_values']['report_item_code'] for s in node['supports']} == {
        'GEO_PL_91750', 'INSTM_PL_91750'}

    retrieval_dir = tmp_path / 'retrieval'
    sync_graph_to_retrieval(merged, retrieval_dir)
    retriever = SubgraphRetriever(retrieval_dir)
    by_canonical = retriever.retrieve('PL_91750', scopes=['public'])
    by_raw = retriever.retrieve('GEO_PL_91750', scopes=['public'])
    assert [n['id'] for n in by_canonical['nodes']] == [node['id']]
    assert [n['id'] for n in by_raw['nodes']] == [node['id']]
    bindings = agent_context(by_raw)['bindings']
    geo_binding = next(b for b in bindings if b['source']['table'] == 'dm_fin_geo_pl')
    assert geo_binding['source_identity'] == {'values': ['GEO_PL_91750'], 'value_types': ['text']}


def test_merger_shared_domain_does_not_leak_across_contributor_order():
    def branch(owner, with_evidence):
        defn = {'type': 'Metric', 'key_names': ['code'], 'identity_scope': 'global',
                'identity_domain': 'shared_dom'}
        if with_evidence:
            defn['identity_evidence_refs'] = ['doc:sec1']
        return {
            'contributor_id': owner, 'kind': 'unit',
            'graph': {
                'format_version': 1, 'target': 'graph', 'status': 'published',
                'support_visibility_version': 1,
                'definitions': {'entities': [defn], 'relations': []},
                'nodes': [{
                    'id': f'{owner}_1', 'type': 'Metric', 'identity': {'code': 'M1'},
                    'identity_domain': 'shared_dom', 'label': '收入', 'properties': {},
                    'supports': [{
                        'scope_ref': 'public', 'label': '收入', 'properties': {},
                        'provenance': {'definition': 'Metric', 'mapping': 0,
                                       'source': {'source_id': owner, 'schema': 'public', 'table': 't'},
                                       'key_columns': ['code'], 'evidence_refs': ['e1']},
                    }],
                }],
                'edges': [], 'evidence_refs': ['e1'],
            },
        }

    a = branch('a_verified', True)
    z = branch('z_unverified', False)
    forward = merge_construction_graphs([a, z])
    reverse = merge_construction_graphs([z, a])
    assert len(forward['nodes']) == 2
    assert len(reverse['nodes']) == 2
    assert forward == reverse


def test_web_execution_after_publication_runs_without_locks_and_current_details_include_catalog(tmp_path):
    workspace = Workspace(tmp_path / 'ws')
    workspace.create('Project', 'alpha')
    config = workspace.result_config('alpha')

    # Verify RunManager._finish(..., notify=False) returns config for lock-free notification.
    manager = RunManager(workspace)
    seen = []
    lock_free = []

    def hook(pid, cfg):
        # Must be able to acquire both locks from another thread without deadlocking.
        acquired = threading.Event()
        def probe():
            with workspace.lock(pid), manager._guard:
                acquired.set()
        t = threading.Thread(target=probe)
        t.start()
        t.join(timeout=2.0)
        lock_free.append(acquired.is_set())
        seen.append((pid, cfg.output_dir))

    manager.after_publication = hook
    rid = 'run_' + 'd' * 24
    result_path = workspace.project_path('alpha') / 'runs' / (rid + '.result.json')
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps({'status': 'completed', 'cleanup_status': 'completed', 'report': None}),
                           encoding='utf-8')
    record = {'run_id': rid, 'status': 'running', 'config_revision': workspace.config_revision('alpha'),
              'dependencies': {'sources': [], 'documents': []}}
    try:
        notify = None
        with workspace.lock('alpha'), manager._guard:
            notify = manager._finish('alpha', {'record': record, 'result': result_path, 'config': config,
                                               'cancel_requested': False, 'forced': False}, notify=False)
            assert seen == []
        if notify is not None and manager.after_publication:
            manager.after_publication('alpha', notify)
        assert len(seen) == 1 and lock_free == [True]
    finally:
        manager.shutdown()

    # Verify graph_history.read_details returns catalog_metadata_notes for current version.
    from lightgraph.contracts import ColumnSpec, TableSpec
    table = TableSpec(source_id='s', schema='public', name='metrics', comment='核心指标表',
                      columns=[ColumnSpec(name='code', data_type='varchar', comment='指标编码'),
                               ColumnSpec(name='cn', data_type='varchar', comment='指标中文名')])
    draft = {
        'draft_id': 'metrics',
        'entities': [{
            'type': 'Metric', 'key_names': ['code'],
            'mappings': [{'source_id': 's', 'table': 'metrics', 'key_columns': ['code'], 'label_column': 'cn'}],
        }],
    }
    with MemorySource([table], {table.qualified_name: [('M1', '营业收入')]}) as src:
        tools = ConstructionTools(config, workspace.project_path('alpha') / 'output' / 'construction', adapters=[src])
        try:
            tools.call('save_graph_draft', yaml_text=dump_yaml(draft))
            assert tools.call('validate_graph_draft', draft_id='metrics')['passed']
            tools.call('publish_graph_draft', draft_id='metrics')
            nid = tools.workspace.read_published_graph()['nodes'][0]['id']
        finally:
            tools.close()
    write_yaml(workspace.project_path('alpha') / 'output' / 'physical_catalog.yaml', {
        'tables': [{'source_id': 's', 'schema': 'public', 'name': 'metrics', 'comment': '核心指标表',
                    'columns': [{'name': 'code', 'data_type': 'varchar', 'comment': '指标编码'},
                                {'name': 'cn', 'data_type': 'varchar', 'comment': '指标中文名'}]}],
    })
    versions = graph_history.list_versions(workspace, 'alpha', config=config)
    current = versions['current_version_id']
    assert current is not None
    details = graph_history.read_details(workspace, 'alpha', config=config, version=current, entity_id=nid)
    std, = details['standard_nodes']
    assert any(note['table_comment'] == '核心指标表' for note in std['catalog_metadata_notes'])
