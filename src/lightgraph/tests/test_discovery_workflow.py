from __future__ import annotations
import json
import httpx
import pytest
from pydantic import ValidationError
from lightgraph.contracts import TableSpec,ColumnSpec,ExtractionGoal,SourceRef
from lightgraph.sources.memory import MemorySource
from lightgraph.discovery import (discover,discover_references,BudgetAllocator,Cost,Hypothesis,
    StructuredModelClient,ModelConfig,ProposalBatch,validate_proposal_references,invalidate_dependencies,revise_after_counterexample)
from lightgraph.errors import LightGraphError,BudgetExceeded
from lightgraph.documents import load_document


def source(columns,rows,name='demo',types=None):
    table=TableSpec(source_id='source',name=name,columns=[ColumnSpec(name=c,data_type=(types or {}).get(c,'text'),comment=c) for c in columns])
    return MemorySource([table],{table.qualified_name:[dict(zip(columns,row)) for row in rows]})


def test_single_level_is_group_and_observations_do_not_expand_identity():
    a=source(['tenant_id','report_item_l2_code','report_item_l2_cn_name','report_item_l2_en_name','sort_order','amount'],
             [['A','02150','销售费用','Sales expense',1,100],['A','02150','销售费用','Sales expense',2,200]])
    result=discover(a)
    assert len(result.rules)==1
    r=result.rules[0]
    assert r.kind=='column_group_entity'
    assert r.identity.key_columns==['tenant_id','report_item_l2_code']
    assert r.identity.semantic_resolution=='source_scoped'
    assert r.attributes.single_valued['label']=='report_item_l2_cn_name'
    assert 'amount' not in r.attributes.single_valued.values()
    assert r.status=='draft' and r.validation_ref is None
    assert '销售费用' not in result.model_dump_json()


def test_wide_hierarchy_requires_independent_direction_evidence():
    a=source(['l1_code','l1_name','l2_code','l2_name'],[['A','甲','B','乙']])
    result=discover(a)
    assert result.rules[0].kind=='wide_hierarchy'
    assert not result.rules[0].hierarchy.order_evidence
    assert result.rules[0].hierarchy.gap_policy=='split'
    assert any(x['code']=='hierarchy_order_semantics_unresolved' for x in result.issues)


def test_documented_hierarchy_retains_fill_and_dependency():
    a=source(['l1_code','l1_name','l2_code','l2_name'],[['A','甲','A','甲']])
    doc=load_document('context.md',text='---\npurpose: data_description\napplies_to: [public.demo]\n---\n# 层级\ndemo 的 L1 到 L2 从父到子；重复编码相同名称表示填充并合并。')
    r=discover(a,documents=[doc]).rules[0]
    assert r.hierarchy.fill_semantics_confirmed
    assert r.hierarchy.order_evidence
    assert doc.document_id+'@'+doc.revision in r.dependencies.documents
    assert r.status=='draft'


def test_source_goal_scope_is_honored():
    a=source(['item_code','item_name'],[['A','甲']])
    assert not discover(a,ExtractionGoal(source_scope=['not_authorized'])).rules


def test_sample_disagreement_is_unresolved_not_silently_repaired():
    a=source(['item_code','item_name'],[['A','甲']],types={'item_code':'integer'})
    result=discover(a)
    assert not result.rules
    assert result.understandings[0].columns[0].conflicts==['declared_numeric_observed_text']
    assert any(x['code']=='schema_value_conflict' for x in result.issues)


def test_conflicting_labels_trigger_full_fd_issue():
    a=source(['item_code','item_name'],[['A','甲'],['A','乙']])
    result=discover(a)
    assert any(x['code']=='fd_violation' for x in result.issues)
    assert result.rules[0].status=='draft'


def test_budget_defers_before_unaffordable_hypothesis():
    a=source(['item_code','item_name'],[['A','甲']])
    b=BudgetAllocator(Cost(db_cu=1,llm_calls=1,llm_tokens=100,memory_bytes=100000,seconds=60))
    result=discover(a,budget=b)
    assert not result.rules and result.issues[0]['status']=='deferred'
    assert a.stats.queries==0


def test_reservations_protect_validation_and_release():
    b=BudgetAllocator(Cost(db_cu=5,llm_calls=2,llm_tokens=100,memory_bytes=1024,seconds=60))
    b.reserve('validation',Cost(db_cu=4))
    with pytest.raises(BudgetExceeded):b.admit(Cost(db_cu=2))
    b.admit(Cost(db_cu=2),'validation');b.charge(Cost(db_cu=2),'validation')
    assert b.snapshot()['reserved']['validation']['db_cu']==2
    b.release('validation');b.admit(Cost(db_cu=3))


def proposal(task='T1_table_discovery'):
    return {'packet_version':'1.0','task':task,'proposals':[{'kind':'column_group','claim':{'group_id':'group','entity_kind_hint':'ReportItem',
        'members':[{'column_id':'c1','role':'identity_code','in_identity':True},{'column_id':'c2','role':'label'}]},
        'evidence_cited':['c1'],'requested_probes':['check_fd']}],'abstain':False}

def packet():
    return {'task':'T1_table_discovery','metadata_digest':{'columns':[{'id':'c1','name':'item_code'},{'id':'c2','name':'item_name'}]},
            'strata_samples':[{'stratum':'ordinary','rows':[{'c1':'DO_NOT_SEND_CANARY'}]}]}


def test_closed_proposals_disallow_status_and_invented_references():
    value=proposal();value['proposals'][0]['claim']['sql']='select *'
    with pytest.raises(ValidationError):ProposalBatch.model_validate(value)
    good=ProposalBatch.model_validate(proposal());good.proposals[0].evidence_cited=['unknown']
    with pytest.raises(LightGraphError,match='dangling_evidence_reference'):validate_proposal_references(good,packet())


def test_sort_column_cannot_enter_identity():
    value=proposal();value['proposals'][0]['claim']['members'][0]['role']='display_order'
    with pytest.raises(ValidationError):ProposalBatch.model_validate(value)


def test_t1_cannot_submit_hierarchy():
    value=proposal();value['task']='T3_hierarchy'
    with pytest.raises(ValidationError):ProposalBatch.model_validate(value)


def test_model_calls_are_bounded_and_metadata_only_strips_samples(monkeypatch):
    monkeypatch.setenv('TEST_LG_KEY','test-secret')
    sent=[]
    def handler(request):
        sent.append(request.content.decode())
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(proposal())},'finish_reason':'stop'}],
                                      'usage':{'prompt_tokens':100,'completion_tokens':100}})
    client=StructuredModelClient(ModelConfig(base_url='https://model.example/v1',model='fake',api_key_env='TEST_LG_KEY'),transport=httpx.MockTransport(handler))
    result=client.propose(packet())
    assert result.proposals and 'DO_NOT_SEND_CANARY' not in sent[0]
    assert client.usage['calls']==1 and client.usage['input_tokens']==100
    assert client.budget.snapshot()['reserved']=={}


def test_model_errors_never_expose_remote_body(monkeypatch):
    monkeypatch.setenv('TEST_LG_KEY','test-secret')
    client=StructuredModelClient(ModelConfig(base_url='https://model.example/v1',model='fake',api_key_env='TEST_LG_KEY'),
        transport=httpx.MockTransport(lambda r:httpx.Response(401,text='REMOTE_BODY_SECRET')))
    with pytest.raises(LightGraphError) as error:client.propose(packet())
    assert str(error.value)=='model_http_error'
    assert 'REMOTE_BODY_SECRET' not in repr(error.value)
    assert client.usage['calls']==1 and client.budget.snapshot()['reserved']=={}


def test_retry_accounting_for_invalid_json(monkeypatch):
    monkeypatch.setenv('TEST_LG_KEY','test-secret')
    n=[]
    def handler(request):
        n.append(1)
        return httpx.Response(200,json={'choices':[{'message':{'content':'bad'}}],'usage':{'prompt_tokens':20,'completion_tokens':10}})
    client=StructuredModelClient(ModelConfig(base_url='https://model.example/v1',model='fake',api_key_env='TEST_LG_KEY'),transport=httpx.MockTransport(handler))
    with pytest.raises(LightGraphError,match='llm_schema_invalid'):client.propose(packet())
    assert len(n)==2 and client.usage['calls']==2
    assert client.budget.used.llm_tokens==60 and client.budget.snapshot()['reserved']=={}


def test_t5_requires_counterexample_and_stale_state_is_explicit():
    h=Hypothesis(hypothesis_id='h_a',kind='column_group',source=SourceRef(source_id='s',table='t'),columns=['code'],dependencies={'schema':'old'})
    with pytest.raises(LightGraphError,match='revision_requires_counterexample'):revise_after_counterexample(None,{},h,[])
    updated=invalidate_dependencies([h],{'schema':'new'})
    assert updated[0].status=='stale' and h.status=='proposed'


def test_unverified_rules_cannot_be_cross_table_targets():
    a=source(['item_code','item_name'],[['A','甲']])
    result=discover_references(a,discover(a).rules)
    assert result.stats['endpoint_rules']==0


def _complex_source(with_discriminator=False,unknown=False):
    from lightgraph.contracts import RuleSpec,IdentitySpec,ScopeSpec,RuleEvidence
    tables=[TableSpec(source_id='s',name=name,columns=[ColumnSpec(name=c) for c in cols]) for name,cols in [
        ('resources',['tenant_id','resource_code','resource_name']),('measures',['tenant_id','measure_code','measure_name']),
        ('links',['tenant_id','resource_code','ref_kind','ref_value'] if with_discriminator else ['tenant_id','resource_code','allowed_measure_codes'])]]
    links=[['A','R','m','["M1","M2"]']] if with_discriminator else [['A','R','["M1","M2"]']]
    if unknown:links.append(['A','R','unrecognized','["M1"]'])
    rows={'public.resources':[('A','R','Resource')],'public.measures':[('A','M1','First'),('A','M2','Second')],'public.links':links}
    rules=[]
    for name,kind,code in [('resources','Metric','resource_code'),('measures','Measure','measure_code')]:
        rules.append(RuleSpec(rule_id=name,kind='column_group_entity',source=SourceRef(source_id='s',table=name),
            scope=ScopeSpec(namespaces=['tenant_id']),identity=IdentitySpec(key_columns=['tenant_id',code],entity_kind=kind),
            status='validated',validation_ref='v_'+name,evidence=RuleEvidence(semantic_basis=['declared'])))
    return MemorySource(tables,rows),rules


def test_json_array_reference_proposal_preserves_structure_not_raw_array():
    a,rules=_complex_source()
    d=load_document('context.md',text='---\npurpose: data_description\napplies_to: [public.links]\n---\n# 引用\nlinks 记录指标使用度量，allowed_measure_codes 是允许引用度量的 JSON 数组。')
    result=discover_references(a,rules,[d])
    assert len(result.rules)==1
    ref=result.rules[0]
    assert ref.relation.target_json_array_columns==['allowed_measure_codes']
    assert ref.relation.target_columns==['tenant_id','allowed_measure_codes']
    assert ref.status=='draft' and ref.evidence.semantic_basis
    assert 'M1' not in ref.model_dump_json()


def test_polymorphic_selector_requires_doc_mapping_and_reports_unknown_partition():
    a,rules=_complex_source(True,True)
    d=load_document('context.md',text='---\npurpose: data_description\napplies_to: [public.links]\n---\n# 引用\nlinks 记录指标使用度量，引用类型 m 表示度量，ref_value 是编码 JSON 数组。')
    result=discover_references(a,rules,[d])
    assert len(result.rules)==1
    ref=result.rules[0]
    assert ref.scope.predicate=={'ref_kind':'m'}
    assert ref.relation.target_json_array_columns==['ref_value']
    unknown=[i for i in result.issues if i['code']=='discriminator_partitions_unresolved']
    assert unknown and unknown[0]['count']==1
    assert 'unrecognized' not in result.model_dump_json()


def test_complex_reference_rejects_missing_target_and_bad_arrays():
    from lightgraph.discovery.references import expand_scalar_array
    with pytest.raises(LightGraphError,match='non_scalar_json_reference'):expand_scalar_array('[{"code":"A"}]')
    with pytest.raises(LightGraphError,match='invalid_json_reference_array'):expand_scalar_array('[1,2,3]',2)
    assert expand_scalar_array('["01",1,"01"]')==['01',1]


def test_long_hierarchy_is_only_proposed_with_parent_declaration():
    a=source(['tenant_id','region_code','region_name','parent_code'],[['A','R0','根',None],['A','R1','地区','R0']],name='region')
    d=load_document('context.md',text='---\npurpose: data_description\napplies_to: [public.region]\n---\n# 字典\nregion 登记区域维度成员，parent_code 指向父成员。')
    rule=discover(a,documents=[d]).rules[0]
    assert rule.kind=='long_hierarchy'
    assert rule.identity.entity_kind=='DimensionMember'
    assert rule.hierarchy.order_evidence and rule.status=='draft'


def test_shared_definition_requires_independent_policy_not_same_name():
    a=source(['tenant_id','item_code','item_name','definition'],[['A','1','费用','完整业务定义'],['B','2','费用','完整业务定义']],name='items')
    assert discover(a).rules[0].identity.semantic_resolution=='source_scoped'
    d=load_document('context.md',text='---\npurpose: data_description\napplies_to: [public.items]\n---\n# 报表项目\nitems 的 definition 表示完整定义。同一完整定义可由多个租户引用。')
    rule=discover(a,documents=[d]).rules[0]
    assert rule.identity.semantic_resolution=='verified_definition_registry'
    assert rule.identity.semantic_namespace.startswith('declared_definition:')
    assert rule.status=='draft' and not rule.validation_ref


def test_discover_engine_shares_only_explicitly_declared_equal_definitions():
    from lightgraph.store import GraphStore
    from lightgraph.rules import RuleEngine
    from lightgraph.contracts import canonical
    columns=['tenant_id','item_code','item_name','definition']
    rows=[['A','1','费用','完整业务定义'],['B','2','费用','完整业务定义'],['B','3','费用','另一业务定义']]
    a=source(columns,rows,name='items')
    doc=load_document('context.md',text='---\npurpose: data_description\napplies_to: [public.items]\n---\n# 报表项目\nitems 的 definition 表示完整定义。同一完整定义可由多个租户引用。')
    shared=discover(a,documents=[doc]).rules[0]
    store=GraphStore();engine=RuleEngine(store)
    outcome=engine.execute(a,shared,document_versions={doc.document_id+'@'+doc.revision})
    assert outcome.published and outcome.counts['entities']==2
    assert engine.bindings[(shared.rule_id,canonical(['A','1']))]==engine.bindings[(shared.rule_id,canonical(['B','2']))]
    assert engine.bindings[(shared.rule_id,canonical(['B','2']))]!=engine.bindings[(shared.rule_id,canonical(['B','3']))]
    store.close()
    independent=discover(a).rules[0]
    store=GraphStore();engine=RuleEngine(store)
    outcome=engine.execute(a,independent)
    assert outcome.published and outcome.counts['entities']==3
    store.close()


def test_definition_fd_conflict_cannot_authorize_shared_identity():
    a=source(['tenant_id','item_code','item_name','definition'],[['A','1','费用','定义一'],['A','1','费用','定义二']],name='items')
    d=load_document('context.md',text='---\npurpose: data_description\napplies_to: [public.items]\n---\n# 报表项目\nitems 中同一完整定义可由多个租户引用。')
    result=discover(a,documents=[d])
    assert result.rules[0].identity.semantic_resolution=='source_scoped'
    assert any(i['code']=='fd_violation' for i in result.issues)


def test_cached_pattern_reuse_detects_new_discriminator_without_model():
    from lightgraph.discovery import validate_cached_patterns
    a,rules=_complex_source(True,True)
    doc=load_document('context.md',text='---\npurpose: data_description\napplies_to: [public.links]\n---\n# 引用\nlinks 记录指标使用度量，引用类型 m 表示度量，ref_value 是编码 JSON 数组。')
    ref=discover_references(a,rules,[doc]).rules[0]
    ref.validation_ref='validated_reference';ref.status='validated'
    review=validate_cached_patterns(a,[ref],[doc])
    assert review.needs_review==[ref.rule_id]
    assert review.issues[0]['code']=='new_discriminator_partition'
    assert review.issues[0]['count']==1
    assert 'unrecognized' not in review.model_dump_json()
    assert review.stats['checked_discriminator_columns']==1


def test_cached_pattern_unchanged_has_no_review_and_no_new_model_calls():
    from lightgraph.discovery import validate_cached_patterns
    a,rules=_complex_source(True)
    doc=load_document('context.md',text='---\npurpose: data_description\napplies_to: [public.links]\n---\n# 引用\nlinks 记录指标使用度量，引用类型 m 表示度量，ref_value 是编码 JSON 数组。')
    ref=discover_references(a,rules,[doc]).rules[0]
    ref.validation_ref='validated_reference';ref.status='validated'
    review=validate_cached_patterns(a,[ref],[doc])
    assert review.needs_review==[] and review.issues==[]


def test_model_input_schema_rejects_hidden_rows_before_network(monkeypatch):
    monkeypatch.setenv('TEST_LG_KEY','fake')
    calls=[]
    client=StructuredModelClient(ModelConfig(base_url='https://model.example/v1',model='fake',api_key_env='TEST_LG_KEY'),
        transport=httpx.MockTransport(lambda r:calls.append(1)))
    unsafe=packet();unsafe['raw_records']=[{'password':'canary'}]
    with pytest.raises(LightGraphError,match='model_packet_schema_invalid'):client.propose(unsafe)
    assert calls==[] and client.usage['calls']==0


def test_structured_model_can_propose_opaque_column_group_without_retaining_rationale(monkeypatch):
    monkeypatch.setenv('TEST_LG_KEY','test-key')
    a=source(['field_a','field_b'],[['A','Name A']])
    def handler(request):
        envelope=json.loads(request.content)
        data=json.loads(envelope['messages'][1]['content'])['input']
        ids={c['name']:c['id'] for c in data['metadata_digest']['columns']}
        response={'task':data['task'],'proposals':[{'kind':'column_group','claim':{'group_id':'opaque','entity_kind_hint':'ReportItem',
            'members':[{'column_id':ids['field_a'],'role':'identity_code','in_identity':True},{'column_id':ids['field_b'],'role':'label'}]},
            'evidence_cited':[ids['field_a'],ids['field_b']],'rationale':'DO_NOT_PERSIST_MODEL_TEXT'}]}
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(response)}}],'usage':{'prompt_tokens':20,'completion_tokens':20}})
    model=StructuredModelClient(ModelConfig(base_url='https://model.example/v1',model='mock',api_key_env='TEST_LG_KEY'),transport=httpx.MockTransport(handler))
    result=discover(a,model=model)
    assert len(result.rules)==1 and result.rules[0].identity.key_columns==['field_a']
    assert model.usage['calls']==1
    assert 'DO_NOT_PERSIST_MODEL_TEXT' not in result.model_dump_json()


def test_t5_runs_only_for_fd_counterexample_and_adds_namespace():
    from types import SimpleNamespace
    class Model:
        config=SimpleNamespace(allow_source_samples=False)
        def __init__(self):self.budget=BudgetAllocator();self.usage={'calls':0};self.tasks=[]
        def propose(self,p):
            self.usage['calls']+=1;self.tasks.append(p['task'])
            if p['task']!='T5_revision':return ProposalBatch(task=p['task'])
            ids={c['name']:c['id'] for c in p['metadata_digest']['columns']}
            return ProposalBatch.model_validate({'task':'T5_revision','proposals':[{'kind':'entity_key',
                'claim':{'group_id':'scoped','entity_kind_hint':'BusinessConcept','key_columns':[ids['business_id'],ids['item_code']],
                         'attributes':{'single_valued':[ids['item_name']]}},'evidence_cited':['fd_check']}]})
    a=source(['business_id','item_code','item_name'],[['A','1','First'],['B','1','Second']])
    model=Model();result=discover(a,model=model)
    assert model.tasks==['T1_table_discovery','T5_revision']
    assert result.rules[0].identity.key_columns==['business_id','item_code']
    clean=source(['business_id','item_code','item_name'],[['A','1','First'],['A','1','First']])
    model=Model();discover(clean,model=model)
    assert model.tasks==['T1_table_discovery']


def test_json_reference_discovery_to_actual_engine_edges():
    from lightgraph.store import GraphStore
    from lightgraph.rules import RuleEngine
    a,rules=_complex_source()
    d=load_document('context.md',text='---\npurpose: data_description\napplies_to: [public.links]\n---\n# 引用\nlinks 记录指标使用度量，allowed_measure_codes 是允许引用度量的 JSON 数组。')
    store=GraphStore();engine=RuleEngine(store)
    for r in rules:assert engine.execute(a,r).published
    result=discover_references(a,store.get_rules(),[d])
    assert len(result.rules)==1
    outcome=engine.execute(a,result.rules[0],document_versions={d.document_id+'@'+d.revision})
    assert outcome.published and outcome.counts['edges']==2
    store.close()
