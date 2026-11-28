"""Regression tests for semantic merge, overlap review, Chinese explanations, retrieval, and Web/UI fixes."""
from copy import deepcopy
import json
import threading

import pytest

from lightgraph.construction.merger import merge_construction_graphs
from lightgraph.construction.overlap_patterns import analyze_overlap_patterns
from lightgraph.construction.overlap_review import review_overlap_patterns
from lightgraph.construction.projection_proposals import verify_projection_proposals
from lightgraph.construction.tools import ConstructionTools
from lightgraph.construction.workspace import dump_yaml, write_yaml
from lightgraph.contracts import digest
from lightgraph.documents.identity_contract import shared_key_claim_refs
from lightgraph.documents.identity_explanations import explanation_claims
from lightgraph.retrieval.agent_context import agent_context
from lightgraph.retrieval.retriever import SubgraphRetriever
from lightgraph.retrieval.sync import sync_graph_to_retrieval
from lightgraph.runtime.config import MergePolicyConfig, RunConfig
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


def test_key_normalizers_fusion_indexes_raw_source_identity_in_retrieval(tmp_path):
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

    inputs = [make_branch('geo_a', 'dm_fin_geo_pl_a', ' GEO_PL_91750 '),
              make_branch('geo_b', 'dm_fin_geo_pl_b', 'geo_pl_91750')]
    policy = {
        'fusion_policy': {
            'entity_rules': [
                {'entity_type': 'ReportItem_geo_a', 'canonical_type': 'ReportItem', 'identity_domain': 'fin_pl',
                 'key_mapping': {'code': 'item_code'}, 'key_normalizers': {'code': ['trim', 'casefold']}},
                {'entity_type': 'ReportItem_geo_b', 'canonical_type': 'ReportItem', 'identity_domain': 'fin_pl',
                 'key_mapping': {'code': 'item_code'}, 'key_normalizers': {'code': ['trim', 'casefold']}},
            ],
        },
    }
    merged = merge_construction_graphs(inputs, merge_policy=policy)
    assert len(merged['nodes']) == 1
    node = merged['nodes'][0]
    assert node['identity'] == {'code': 'geo_pl_91750'}
    assert {s['original_key_values']['report_item_code'] for s in node['supports']} == {
        ' GEO_PL_91750 ', 'geo_pl_91750'}

    retrieval_dir = tmp_path / 'retrieval'
    sync_graph_to_retrieval(merged, retrieval_dir)
    retriever = SubgraphRetriever(retrieval_dir)
    by_canonical = retriever.retrieve('geo_pl_91750', scopes=['public'])
    by_raw = retriever.retrieve('GEO_PL_91750', scopes=['public'])
    assert [n['id'] for n in by_canonical['nodes']] == [node['id']]
    assert [n['id'] for n in by_raw['nodes']] == [node['id']]
    bindings = agent_context(by_raw)['bindings']
    raw_binding = next(b for b in bindings if b['source']['table'] == 'dm_fin_geo_pl_a')
    assert raw_binding['source_identity'] == {'values': [' GEO_PL_91750 '], 'value_types': ['text']}


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


def test_physical_projection_allows_asymmetric_identity_domain_but_rejects_conflicts():
    # u1 declares global + identity_domain, u2 leaves identity_scope='unknown' + identity_domain=None
    # on the exact same physical table and key columns -> should verify and collapse cleanly.
    data = [unit('u1', 'DeliveryRegion', 'zone', field='capacity'),
            unit('u2', 'ReportingArea', 'area', field='language')]
    data[1]['graph']['definitions']['entities'][0]['identity_scope'] = 'unknown'
    data[1]['graph']['definitions']['entities'][0].pop('identity_domain', None)
    for node in data[1]['graph']['nodes']:
        node.pop('identity_domain', None)
    policy, report = _compile(data, 'same_physical_key')
    assert report['verified_groups'] == 1, report
    merged = merge_construction_graphs(data, merge_policy=policy)
    assert merged['fusion_diagnostics']['model_verified_projections']['collapsed_nodes'] == 1

    # Negative guard 1: two conflicting non-empty domains still fail incompatible_identity_contract.
    conflict_domain = [unit('u1', 'DeliveryRegion', 'zone', field='capacity'),
                       unit('u2', 'ReportingArea', 'area', field='language')]
    conflict_domain[1]['graph']['definitions']['entities'][0]['identity_domain'] = 'other_domain'
    for node in conflict_domain[1]['graph']['nodes']:
        node['identity_domain'] = 'other_domain'
    _, conflict_report = _compile(conflict_domain, 'same_physical_key')
    assert conflict_report['verified_groups'] == 0
    assert conflict_report['rejected_keys_by_reason']['incompatible_identity_contract'] == 1

    # Negative guard 2: scoped vs global still fails incompatible_identity_contract.
    conflict_scope = [unit('u1', 'DeliveryRegion', 'zone', field='capacity'),
                      unit('u2', 'ReportingArea', 'area', field='language')]
    conflict_scope[1]['graph']['definitions']['entities'][0]['identity_scope'] = 'scoped'
    conflict_scope[1]['graph']['definitions']['entities'][0].pop('identity_domain', None)
    for node in conflict_scope[1]['graph']['nodes']:
        node.pop('identity_domain', None)
    _, scope_report = _compile(conflict_scope, 'same_physical_key')
    assert scope_report['verified_groups'] == 0


def test_hierarchy_replay_survives_cross_family_ordinary_edges_and_declared_cross_type_hierarchies():
    from lightgraph.construction.executor import execute_draft
    from lightgraph.contracts import ColumnSpec, TableSpec
    from lightgraph.tests.test_source_hierarchy_replays import contributor, draft_graph

    # Scenario A: Valid hierarchy members that ALSO carry healthy ordinary edges to Metric
    # must not be poisoned into incompatible_endpoint_or_role.
    g1 = draft_graph(1, 'Heading')
    g2 = draft_graph(2, 'Term', inverse=True)
    for idx, (graph, kind) in enumerate(((g1, 'Heading'), (g2, 'Term'))):
        metric_id = f'metric_{idx}'
        graph['definitions']['entities'].append({'type': 'Metric', 'key_names': ['mcode'], 'identity_scope': 'unknown'})
        graph['definitions']['relations'].append({
            'type': f'measured_by_{idx}', 'source_type': kind, 'target_type': 'Metric',
            'hierarchy_orientation': 'none',
        })
        graph['nodes'].append({
            'id': metric_id, 'type': 'Metric', 'identity': {'mcode': 'M1'}, 'identity_types': {'mcode': 'text'},
            'label': '收入', 'properties': {}, 'supports': [{
                'scope_ref': 'public', 'label': '收入', 'properties': {},
                'provenance': {'definition': 'Metric', 'mapping': 0,
                               'source': {'source_id': 'file', 'schema': 'public', 'table': 'vocabulary'},
                               'key_columns': ['section'], 'label_column': 'caption'},
            }],
        })
        root = next(n for n in graph['nodes'] if n['type'] == kind and next(iter(n['identity'].values())) == 'X')
        graph['edges'].append({
            'id': f'e_metric_{idx}', 'type': f'measured_by_{idx}', 'source': root['id'], 'target': metric_id,
            'supports': [{
                'scope_ref': 'public',
                'provenance': {'definition': f'measured_by_{idx}', 'mapping': 0,
                               'source': {'source_id': 'file', 'schema': 'public', 'table': 'vocabulary'},
                               'source_key_columns': ['section'], 'target_key_columns': ['section']},
            }],
        })
    merged = merge_construction_graphs([contributor('u1', g1), contributor('u2', g2)])
    assert merged['fusion_diagnostics']['hierarchy_replay_collapsed_nodes'] == 3
    hierarchy_nodes = [n for n in merged['nodes'] if n['type'] != 'Metric']
    assert len(hierarchy_nodes) == 3

    # Scenario B: Unit 1 models two distinct level types (L1Item -> L2Item) with declared parent_to_child,
    # while Unit 2 models a single Item -> Item hierarchy on the same physical table.
    table = TableSpec(source_id='file', name='vocabulary',
                      columns=[ColumnSpec(name=c) for c in ('section', 'caption', 'leaf', 'heading')])
    rows = [('X', 'Catalogue', 'A-01', 'Inscription'), ('X', 'Catalogue', 'B-01', 'Inscription')]
    binding = {'source_id': 'file', 'table': 'vocabulary'}
    cross_type_draft = {
        'draft_id': 'cross_type_hier',
        'entities': [
            {'type': 'L1Item', 'key_names': ['code'], 'identity_scope': 'global',
             'identity_domain': 'ns_l1', 'identity_evidence_refs': ['e10'],
             'mappings': [{**binding, 'key_columns': ['section'], 'label_column': 'caption'}]},
            {'type': 'L2Item', 'key_names': ['code'], 'identity_scope': 'global',
             'identity_domain': 'ns_l2', 'identity_evidence_refs': ['e10'],
             'mappings': [{**binding, 'key_columns': ['leaf'], 'label_column': 'heading'}]},
        ],
        'relations': [{
            'type': 'contains_l2', 'source_type': 'L1Item', 'target_type': 'L2Item',
            'hierarchy_orientation': 'parent_to_child',
            'mappings': [{**binding, 'source_key_columns': ['section'], 'target_key_columns': ['leaf']}],
        }],
    }
    with MemorySource([table], {table.qualified_name: rows}) as adapter:
        built = execute_draft(cross_type_draft, [adapter], [table])
    assert built['passed'], built['diagnostics']
    g_cross = {**built['graph'], 'draft_id': 'cross_type_hier', 'draft_revision': 1}
    merged_cross = merge_construction_graphs([
        contributor('u_cross', g_cross),
        contributor('u_single', draft_graph(2, 'Item', inverse=True)),
    ])
    assert merged_cross['fusion_diagnostics']['hierarchy_replay_collapsed_nodes'] == 3
    assert len(merged_cross['nodes']) == 3


def test_redundant_label_in_composite_key_escalates_to_warning_and_parallel_unit_notes_shared_tables():
    from lightgraph.construction.executor import execute_draft
    from lightgraph.construction.quality import name_mapping_advice
    from lightgraph.construction.workflow import build_unit_config
    from lightgraph.contracts import ColumnSpec, TableSpec
    from lightgraph.runtime.config import WorkflowConfig, WorkflowUnitConfig

    table = TableSpec(source_id='s', name='items', columns=[ColumnSpec(name='code'), ColumnSpec(name='cn')])
    draft = {
        'draft_id': 'items',
        'entities': [{
            'type': 'Item', 'key_names': ['code', 'cn'],
            'mappings': [{'source_id': 's', 'table': 'items', 'key_columns': ['code', 'cn'], 'label_column': 'cn'}],
        }],
    }
    # 1:1 code -> name: label in composite key is provably redundant -> warning!
    with MemorySource([table], {table.qualified_name: [('C1', '收入'), ('C2', '成本')]}) as src:
        unique_res = execute_draft(draft, [src], [table])
    advice_warn, = name_mapping_advice(draft, unique_res['graph'])
    assert advice_warn['code'] == 'primary_name_in_composite_key'
    assert advice_warn['severity'] == 'warning'
    assert advice_warn['redundant_label_in_key'] is True
    assert advice_warn['non_label_keys_with_multiple_names'] == 0

    # 1:N code -> multiple names: composite key is justified -> suggestion!
    with MemorySource([table], {table.qualified_name: [('C1', '收入'), ('C1', '净收入')]}) as src:
        multi_res = execute_draft(draft, [src], [table])
    advice_ok, = name_mapping_advice(draft, multi_res['graph'])
    assert advice_ok['code'] == 'primary_name_in_composite_key'
    assert advice_ok['severity'] == 'suggestion'
    assert advice_ok['redundant_label_in_key'] is False
    assert advice_ok['non_label_keys_with_multiple_names'] == 1

    # Shared tables across parallel workflow units are highlighted in build_unit_config.
    cfg = RunConfig(
        sources=[
            {'id': 'db1', 'kind': 'csv_file', 'path': 'a.csv', 'table_name': 't1', 'encoding': 'utf-8',
             'delimiter': ',', 'header': True, 'null_values': [''], 'type_inference': 'text'},
            {'id': 'shared_xlsx', 'kind': 'csv_file', 'path': 's.csv', 'table_name': 'Sheet1', 'encoding': 'utf-8',
             'delimiter': ',', 'header': True, 'null_values': [''], 'type_inference': 'text'},
        ],
        model={'mode': 'in_environment'},
        workflow=WorkflowConfig(
            enabled=True,
            units=[
                WorkflowUnitConfig(unit_id='u1', source_ids=['db1', 'shared_xlsx'],
                                   source_table_overrides={'shared_xlsx': ['Sheet1']}),
                WorkflowUnitConfig(unit_id='u2', source_ids=['shared_xlsx'],
                                   source_table_overrides={'shared_xlsx': ['Sheet1']}),
            ],
        ),
    )
    scoped = build_unit_config(cfg, cfg.workflow.units[0], unit_output_dir='/tmp/u1')
    assert '跨单元共享来源约定' in scoped.goal_markdown
    assert 'shared_xlsx.Sheet1' in scoped.goal_markdown


def test_long_workspace_paths_work_across_workspace_analytics_retention_and_retrieval(tmp_path):
    from lightgraph.construction import analytics_store, paths
    from lightgraph.runtime.retention import atomic_text
    from lightgraph.web.deletion import delete_project

    # Construct a >265-char workspace root (each segment well below NAME_MAX) to exercise deep Windows paths.
    deep_root = tmp_path
    while len(str(deep_root)) < 270:
        deep_root = deep_root / ('nested_segment_' + 'x' * 30)
    ws = Workspace(deep_root)
    assert not str(ws.root).startswith('\\\\?\\')
    ws.create('DeepPathProject', 'deep_p')
    cfg = ws.result_config('deep_p')
    run_id = 'run_' + 'e' * 24
    ws.select_output_generation('deep_p', run_id, create=True)
    cfg = ws.result_config('deep_p')
    assert len(cfg.output_dir) > 270

    # Exercise atomic_text, analytics persistence, and retrieval sync under the >270-char lineage.
    marker_file = ws.generation_path('deep_p', run_id) / 'workflow-runs' / (run_id + '.json')
    atomic_text(marker_file, json.dumps({'ok': True}))
    assert json.loads(paths.io_path(marker_file).read_text(encoding='utf-8')) == {'ok': True}

    sample_graph = {
        'format_version': 1, 'target': 'graph', 'status': 'published',
        'support_visibility_version': 1, 'version_id': 'v_' + '1' * 32,
        'definitions': {'entities': [{'type': 'Item', 'key_names': ['code']}], 'relations': []},
        'nodes': [{'id': 'n1', 'type': 'Item', 'identity': {'code': 'A1'}, 'label': '项目一',
                   'properties': {}, 'supports': [{'scope_ref': 'public', 'label': '项目一', 'properties': {},
                   'provenance': {'definition': 'Item', 'mapping': 0,
                                  'source': {'source_id': 's', 'schema': 'public', 'table': 't'},
                                  'key_columns': ['code'], 'evidence_refs': ['e1']}}]}],
        'edges': [], 'evidence_refs': ['e1'],
    }
    ap = analytics_store.artifact_path(ws.generation_path('deep_p', run_id) / 'construction', cfg, 'v_' + '1' * 32, None)
    analytics_store.persist_analysis(sample_graph, cfg, None, ap)
    loaded_analytics = analytics_store.read_analysis(
        ws.generation_path('deep_p', run_id) / 'construction', cfg, 'v_' + '1' * 32, None)
    assert loaded_analytics['counts']['nodes'] == 1

    retrieval_dir = ws.generation_path('deep_p', run_id) / 'retrieval'
    sync_graph_to_retrieval(sample_graph, retrieval_dir)
    retriever = SubgraphRetriever(retrieval_dir)
    assert [n['id'] for n in retriever.retrieve('A1', scopes=['public'])['nodes']] == ['n1']
    # Release lease before project deletion.
    if retriever._build_lease is not None:
        retriever._build_lease.close()
    assert delete_project(ws, 'deep_p')['deleted'] is True


def test_hub_entity_with_over_16_roles_preserves_shared_sheet_binding_for_propose():
    from lightgraph.construction.overlap_patterns import analyze_indexed_overlap_patterns, build_overlap_index
    from lightgraph.construction.overlap_review import _structural_profile

    sheet = {'source_id': 's_excel', 'schema': 'public', 'table': 'Sheet1'}

    def make_unit(owner, key_name, rel_name, extra_sql_roles):
        dm = {'source_id': 'gauss', 'schema': 'dm_fin', 'table': f'dm_fin_{owner}'}
        parent_supports = [{
            'scope_ref': 'public', 'label': '营业收入', 'properties': {},
            'business_context': {'status': 'global', 'dimensions': {}},
            'provenance': {'source': sheet, 'key_columns': ['REPORT_ITEM_L1_CODE'],
                           'label_column': 'REPORT_ITEM_L1_CN_NAME'},
        }]
        if extra_sql_roles:
            parent_supports.append({
                'scope_ref': 'public', 'label': '营业收入', 'properties': {},
                'business_context': {'status': 'global', 'dimensions': {}},
                'provenance': {'source': dm, 'key_columns': ['metriccode'], 'label_column': 'metricname'},
            })
        child_supports = [{
            'scope_ref': 'public', 'label': '主营业务收入', 'properties': {},
            'business_context': {'status': 'global', 'dimensions': {}},
            'provenance': {'source': sheet, 'key_columns': ['REPORT_ITEM_L2_CODE'],
                           'label_column': 'REPORT_ITEM_L2_CN_NAME'},
        }]
        nodes = [
            {'id': f'{owner}_p', 'type': 'indicator_member', 'identity': {key_name: 'PL_100'},
             'identity_types': {key_name: 'text'}, 'label': '营业收入', 'properties': {},
             'supports': parent_supports},
            {'id': f'{owner}_c', 'type': 'indicator_member', 'identity': {key_name: 'PL_101'},
             'identity_types': {key_name: 'text'}, 'label': '主营业务收入', 'properties': {},
             'supports': child_supports},
        ]
        edges = [{
            'id': f'{owner}_hier', 'type': rel_name, 'source': f'{owner}_p', 'target': f'{owner}_c',
            'supports': [{'scope_ref': 'public', 'provenance': {
                'source': sheet, 'source_key_columns': ['REPORT_ITEM_L1_CODE'],
                'target_key_columns': ['REPORT_ITEM_L2_CODE']}}],
        }]
        rel_defs = [{'type': rel_name, 'source_type': 'indicator_member', 'target_type': 'indicator_member'}]
        ent_defs = [{'type': 'indicator_member', 'key_names': [key_name], 'identity_scope': 'unknown'}]
        for i in range(extra_sql_roles):
            dim_type = f'aa_dim_{i:02d}'
            rel_type = f'aa_rel_{i:02d}'
            dim_col = f'dim_{i:02d}_code'
            ent_defs.append({'type': dim_type, 'key_names': [dim_col], 'identity_scope': 'unknown'})
            rel_defs.append({'type': rel_type, 'source_type': 'indicator_member', 'target_type': dim_type})
            nodes.append({
                'id': f'{owner}_d{i}', 'type': dim_type, 'identity': {dim_col: f'D{i}'},
                'identity_types': {dim_col: 'text'}, 'label': f'维度{i}', 'properties': {},
                'supports': [{'scope_ref': 'public', 'label': f'维度{i}', 'properties': {},
                              'business_context': {'status': 'global', 'dimensions': {}},
                              'provenance': {'source': dm, 'key_columns': [dim_col],
                                             'label_column': f'dim_{i:02d}_name'}}],
            })
            edges.append({
                'id': f'{owner}_re{i}', 'type': rel_type, 'source': f'{owner}_p', 'target': f'{owner}_d{i}',
                'supports': [{'scope_ref': 'public', 'provenance': {
                    'source': dm, 'source_key_columns': ['metriccode'], 'target_key_columns': [dim_col]}}],
            })
        return {'kind': 'unit', 'source_namespace': 'ws1', 'contributor_id': owner,
                'graph': {'access_fingerprint': f'fp_{owner}', 'status': 'published',
                          'support_visibility_version': 1, 'nodes': nodes, 'edges': edges,
                          'definitions': {'entities': ent_defs, 'relations': rel_defs}}}

    branches = [make_unit('e2', 'code', 'indicator_has_sub_indicator', 20),
                make_unit('e3', 'indicator_code', 'indicator_hierarchy', 0)]
    indexed = [{k: v for k, v in b.items() if k != 'graph'} | {'overlap_index': build_overlap_index(b)}
               for b in branches]
    analysis = analyze_indexed_overlap_patterns(indexed)
    card = next(p for p in analysis.patterns
                if p['basis'] == 'same_physical_key'
                and p['witness_binding']['key_columns'] == ['REPORT_ITEM_L1_CODE'])
    profile = _structural_profile(card)
    assert 'propose' in profile['review_actions'], profile
    assert {c['member_index'] for c in profile['binding_choices']} == {0, 1}
    assert any(r['key_columns'] == ['REPORT_ITEM_L1_CODE'] and r['source_indexes']
               for r in card['members'][0]['roles'])
    assert card['binding_comparisons']['rows']


def test_observed_identity_normalizes_tautological_key_is_not_null_predicate_only():
    from lightgraph.tests.test_observed_identity import projection, unit as obs_unit

    left = obs_unit('e2', projection())
    right = obs_unit('e3', projection('经营指标', '指标编码'))
    for node in left['graph']['nodes']:
        node['supports'][0]['provenance']['predicate'] = {
            'all': [{'op': 'is_not_null', 'column': 'REPORT_CODE'},
                    {'op': 'not_null', 'column': 'REPORT_NAME'}],
        }
    left['graph']['edges'][0]['supports'][0]['provenance']['predicate'] = {
        'op': 'is_not_null', 'column': 'PARENT_CODE',
    }
    merged = merge_construction_graphs([left, right])
    assert len(merged['nodes']) == 2 and len(merged['edges']) == 1
    assert merged['fusion_diagnostics']['source_replay_collapsed_nodes'] == 2

    # Non-key predicate must still prevent source replay collapse.
    filtered = deepcopy(left)
    for node in filtered['graph']['nodes']:
        node['supports'][0]['provenance']['predicate'] = {'op': 'is_not_null', 'column': 'extra_flag'}
    separated = merge_construction_graphs([filtered, right])
    assert len(separated['nodes']) == 4


def test_deterministic_stage_inputs_reuses_completed_with_gaps_complete_checkpoints(tmp_path):
    from lightgraph.construction.checkpoints import commit_checkpoint, load_checkpoint
    from lightgraph.construction.workflow import deterministic_stage_inputs, effective_merge_policy, resolve_workflow_plan
    from lightgraph.tests.test_source_hierarchy_replays import contributor as hier_contributor, draft_graph

    units = {name: hier_contributor(name, draft_graph(i, kind)) for i, (name, kind) in enumerate(
        [('one', 'Entry'), ('two', 'Term'), ('three', 'Subject')], start=1)}
    parents = {'ab': ['one', 'two'], 'bc': ['two', 'three'], 'root': ['ab', 'bc']}
    config = RunConfig(
        sources=[{'id': 'file', 'kind': 'excel_workbook', 'path': 'unused.xlsx'}],
        workflow={'enabled': True, 'units': [{'unit_id': name, 'source_ids': ['file']} for name in units],
                  'merge_nodes': [{'node_id': name} for name in parents],
                  'edges': [{'edge_id': name + parent, 'source': parent, 'target': name}
                            for name, incoming in parents.items() for parent in incoming]},
    )
    run_id = 'run_' + '2' * 24
    plan = resolve_workflow_plan(config.workflow)
    nodes = {n.node_id: n for n in config.workflow.merge_nodes}
    policy = effective_merge_policy(config)
    artifacts = {name: commit_checkpoint(tmp_path, run_id, name, u, input_digest=name, graph_kind='node_delta')
                 for name, u in units.items()}
    loaded_units = {name: load_checkpoint(tmp_path, record) for name, record in artifacts.items()}
    artifacts['one']['status'] = 'completed_with_gaps'
    merged = {}
    for name in ['ab', 'bc']:
        stage_inputs = [loaded_units[parent] for parent in parents[name]]
        stage = {'contributor_id': name, 'kind': 'merged',
                 'graph': merge_construction_graphs(stage_inputs, merge_policy=policy)}
        artifacts[name] = commit_checkpoint(
            tmp_path, run_id, name, stage, input_digest=name,
            upstream_artifacts=[artifacts[parent]['artifact_id'] for parent in parents[name]],
        )
        merged[name] = load_checkpoint(tmp_path, artifacts[name])
    artifacts['ab']['status'] = 'completed_with_gaps'
    reused = deterministic_stage_inputs(
        parents['root'], units=loaded_units, merged=merged, merge_nodes=nodes,
        plan=plan, artifacts=artifacts, policy=policy, config=config, bases=[],
    )
    assert {c['contributor_id'] for c in reused} == set(units)
    artifacts['ab']['coverage'] = 'incomplete'
    fallback = deterministic_stage_inputs(
        parents['root'], units=loaded_units, merged=merged, merge_nodes=nodes,
        plan=plan, artifacts=artifacts, policy=policy, config=config, bases=[],
    )
    assert fallback == [merged['ab'], merged['bc']]
