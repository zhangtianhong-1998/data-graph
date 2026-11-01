"""Relation time, flat-table tools, retained labels and duplicate fact-table entities."""
from types import SimpleNamespace

from lightgraph.contracts import ExtractionGoal, RuleSpec
from lightgraph.discovery.budget import BudgetAllocator, Cost
from lightgraph.discovery.proposal_session import ProposalSession, hierarchy_shape_available, proposal_tool_specs
from lightgraph.discovery.schema_knowledge import drop_foreign_identity_copies, project_retained_labels
from lightgraph.pipeline import relation_attempt_consumed
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import group, table
from lightgraph.tests.test_schema_knowledge import packet, setup_ledger


def test_phase_deadline_moves_inside_the_run_ceiling(monkeypatch):
    now=[100.0]
    monkeypatch.setattr('lightgraph.discovery.budget.time.monotonic', lambda: now[0])
    budget=BudgetAllocator(Cost(seconds=20))
    assert budget.bind_run_deadline(112.0)==112.0
    assert budget.set_phase_deadline(106.0)==106.0
    assert budget.set_phase_deadline(111.0)==111.0
    assert budget.set_phase_deadline(130.0)==112.0


def test_clock_stop_does_not_consume_a_relation_attempt():
    stopped=SimpleNamespace(rules=[], issues=[{'code':'discovery_time_budget_exhausted'}])
    attempted=SimpleNamespace(rules=[], issues=[{'code':'model_abstained'}])
    produced=SimpleNamespace(rules=['edge'], issues=[{'code':'discovery_time_budget_exhausted'}])
    assert relation_attempt_consumed(stopped) is False
    assert relation_attempt_consumed(attempted) is True
    assert relation_attempt_consumed(produced) is True


def test_flat_definition_table_has_no_hierarchy_plan():
    body={'task':'T0_discovery','metadata_digest':{'columns':[
        {'id':'c_code','name':'渠道编码'},{'id':'c_name','name':'渠道名称'},{'id':'c_note','name':'渠道说明'}]}}
    assert hierarchy_shape_available(body) is False
    names={spec['function']['name'] for spec in proposal_tool_specs('T0_discovery', body)}
    assert 'submit_hierarchy_plan' not in names
    assert 'submit_long_hierarchy' in names  # An adjacency list need not have numbered levels.
    assert 'submit_column_group' in names
    rejected=ProposalSession(body).execute('submit_hierarchy_plan', {'plan_id':'copied'})
    assert rejected['code']=='hierarchy_shape_not_on_table'
    assert rejected['diagnostics'][0]['allowed_references']==['c_code','c_name','c_note']


def test_level_columns_keep_the_hierarchy_plan():
    body={'task':'T0_discovery','metadata_digest':{'columns':[
        {'id':'c1','name':'FRUIT_ITEM_L1_CODE'},{'id':'c2','name':'FRUIT_ITEM_L2_CODE'}]}}
    names={spec['function']['name'] for spec in proposal_tool_specs('T0_discovery', body)}
    assert 'submit_hierarchy_plan' in names


def test_evidence_rejection_lists_this_tables_definition_sections():
    current=table('items',['item_code','item_cn_name'])
    body=packet(current)
    body['scope']={'source_id':current.source_id,'schema':current.schema_name,'table':current.name}
    body['document_snippets']=[{
        'doc_id':'definition','revision':'1','section_id':'fields',
        'evidence_role':'definition_evidence','read_state':'complete',
        'scope':{'tables':['public.items'],'source_ids':[current.source_id]}}]
    session=ProposalSession(body)
    claim=group(body, 'item_code', 'item_cn_name')['proposals'][0]
    claim.pop('kind')
    claim['evidence_cited']=['other_table@1:fields']
    rejected=session.execute('submit_column_group', claim)
    assert rejected['code']=='dangling_evidence_reference'
    assert rejected['definition_sections']==['definition@1:fields']


def _entity(rule_id, table_name, *, label=None):
    return RuleSpec(rule_id=rule_id, kind='column_group_entity',
        source={'source_id':'s','table':table_name},
        identity={'key_columns':['指标编码'],'entity_kind':'Metric','identity_domain':'fruit_metric_code'},
        attributes={'single_valued':{'label':label} if label else {}})


def test_fact_table_code_is_not_a_second_entity_when_the_definition_has_a_label():
    definition=_entity('metric','指标定义', label='指标名称')
    copy=_entity('context','指标产品渠道')
    assert drop_foreign_identity_copies([copy, definition])=={'context':'指标产品渠道'}
    assert drop_foreign_identity_copies([definition])=={}


def test_retained_name_column_is_projected_before_publish(tmp_path):
    current=table('渠道定义',['渠道编码','渠道名称','渠道说明'])
    rule=RuleSpec(rule_id='channel', kind='column_group_entity',
        source={'source_id':current.source_id,'schema':current.schema_name,'table':current.name},
        identity={'key_columns':['渠道编码'],'entity_kind':'Channel','identity_domain':'fruit_channel_code'},
        attributes={'single_valued':{'channel_service_description':'渠道说明'}})
    with GraphStore(tmp_path/'knowledge.sqlite') as store:
        ledger=setup_ledger(store, [current], ExtractionGoal())
        assert project_retained_labels([rule], ledger)==1
    assert rule.attributes.single_valued['label']=='渠道名称'
    assert rule.attributes.single_valued['channel_service_description']=='渠道说明'
