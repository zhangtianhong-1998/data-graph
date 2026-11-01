"""A retained hierarchy field model becomes a rule without retyping column ids."""
from types import SimpleNamespace

from lightgraph.contracts import ExtractionGoal, RuleSpec
from lightgraph.discovery.budget import BudgetAllocator
from lightgraph.discovery.engine import _definition_document
from lightgraph.discovery.models import ProposalBatch
from lightgraph.discovery.schema_knowledge import proposals_from_field_models
from lightgraph.discovery.field_model_plan import compile_field_model_plan
from lightgraph.discovery.semantics import BoundSemanticVerdict
from lightgraph.discovery.validation_feedback import model_diagnostics
from lightgraph.discovery.workflow import discover_with_agent
from lightgraph.documents import load_document
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_object_model_supersession import fixture
from lightgraph.tests.test_schema_knowledge import packet, setup_ledger
from pydantic import ValidationError

from lightgraph.discovery.schema_knowledge import ObjectModelDraft

DEFINITION = 'paths@1:guide'


class ReviewModel:
    def __init__(self):
        self.budget=BudgetAllocator()
        self.config=SimpleNamespace(max_discovery_steps=4,max_tool_calls=4,
            allow_source_samples=False,max_prompt_bytes=262144,response_format='json_schema',
            max_output_tokens=1024)
        self.usage={'calls':0}

    def explore(self, packet, tools, **kwargs):
        scope=packet.get('scope')
        if scope and hasattr(tools,'begin_context'):
            from lightgraph.contracts import SourceRef
            tools.begin_context([SourceRef.model_validate(scope)])
        if packet['task']=='T3_hierarchy':
            obj=next(o for o in packet['schema_knowledge']['objects'] if len(o.get('positions') or [])>=2)
            evidence=[d['doc_id']+'@'+d['revision']+':'+d['section_id'] for d in packet['document_snippets']]
            return compile_field_model_plan(dict(object_id=obj['object_id'],plan_id='path',entity_type='ReportItem',
                identity_scope='namespace',shared_identity_domain='paths',position_meaning='relative_ordinal',
                allow_multiple_parents=False,repeat_is_fill=True,relation_type='parent_of',evidence=evidence),packet)
        return ProposalBatch(task=packet['task'],abstain=True,proposals=[])

    def complete_bound_review(self, packet, *, proposition_ref, allow_schema_repair=True):
        evidence=[{'reference':ref,'stance':'supports'}
            for ref in packet.get('required_document_refs') or [packet['metadata_digest']['columns'][0]['id']]]
        return BoundSemanticVerdict(decision='supported', evidence=evidence).bind(proposition_ref)


def test_field_model_keeps_position_column_ids_without_inventing_semantics():
    source,knowledge,model=fixture()
    stored={**model,'status':'candidate','object_id':model['object_id'],'position_semantics':'ordinal_slots'}
    assert proposals_from_field_models([stored],[DEFINITION])==[]
    body=packet(source)
    body.update(scope={'source_id':source.source_id,'schema':source.schema_name,'table':source.name},
        schema_knowledge={'objects':[stored]})
    supplied=dict(object_id=model['object_id'],plan_id='explicit',entity_type='ReportItem',identity_scope='source',
        position_meaning='relative_ordinal',allow_multiple_parents=True,repeat_is_fill=True,
        relation_type='parent_of',evidence=['definition@1:fields'])
    proposal=compile_field_model_plan(supplied,body).proposals[0]
    claim=proposal.claim
    assert [level.key for level in claim.levels]==[item['code'] for item in model['positions']]
    assert [level.label for level in claim.levels]==[item['label'] for item in model['positions']]
    assert all(level.aliases==position['aliases'] for level,position in zip(claim.levels,model['positions']))
    assert claim.identity_domain.startswith('plan_source:') and claim.allow_dag is True
    assert claim.namespace_columns==model['namespace_columns']


def test_abstaining_agent_still_compiles_retained_hierarchy(tmp_path):
    table,_,model=fixture()
    model={**model,'position_semantics':'ordinal_slots'}
    guide=load_document('paths.md',text='''---
id: paths
purpose: data_description
applies_to: ["s:public.paths"]
---
# 层级
同一编码在不同位置是同一报表项。编号位置保持原位，连续重复编码是填充。
''')
    rows=[{'tenant_id':'T',
        **{f'item_l{level}_{suffix}':value for level,value in ((1,'A'),(2,'B'))
           for suffix,value in (('code',value),('cn_name','Name '+value),('en_name','En '+value),('alias','Al '+value))}}]
    # The fixture table has five levels. Fill the unused ones with the leaf.
    row=rows[0]
    for level in range(3,6):
        for suffix,value in (('code','B'),('cn_name','Name B'),('en_name','En B'),('alias','Al B')):
            row[f'item_l{level}_{suffix}']=value
    with GraphStore(tmp_path/'knowledge.sqlite') as store:
        ledger=setup_ledger(store,[table])
        body=packet(table)
        body['schema_knowledge']=ledger.context(table)
        from lightgraph.discovery.proposal_session import ProposalSession
        session=ProposalSession(body)
        assert session.execute('declare_object_model',model)['status']=='ok'
        ledger.record_models(table,session.batch(abstain=True)._runtime_object_models,body)
        with MemorySource([table],{table.qualified_name:[row]}) as source:
            result=discover_with_agent(source,ExtractionGoal(),documents=[guide],
                model=ReviewModel(),schema_knowledge=ledger)
    assert any(rule.kind=='wide_hierarchy' for rule in result.rules)
    rule=next(rule for rule in result.rules if rule.kind=='wide_hierarchy')
    assert [level.code for level in rule.hierarchy.levels]==[
        'item_l1_code','item_l2_code','item_l3_code','item_l4_code','item_l5_code']
    assert rule.identity.identity_domain.startswith('plan_shared:')
    assert all(ref.startswith('paths@') for ref in rule.identity.identity_evidence if '@' in ref)


def test_overlap_constraint_is_named_without_the_rejected_value():
    raw={'object_id':'same','key_columns':['c_code'],'label_columns':['c_code']}
    try:
        ObjectModelDraft.model_validate(raw)
    except ValidationError as error:
        diagnostics=model_diagnostics(error,ObjectModelDraft.model_json_schema(),raw)
    else:
        raise AssertionError('overlap should fail validation')
    assert diagnostics[0]['constraint']=='object_model_code_label_overlap'
    assert 'c_code' not in str(diagnostics)


def test_definition_scope_rejects_another_table():
    hierarchy=SimpleNamespace(source_id='fruit',schema_name='public',name='报表项层级')
    product=SimpleNamespace(source_id='fruit',schema_name='public',name='产品定义')
    doc={'evidence_role':'definition_evidence','read_state':'complete',
         'scope':{'source_ids':['fruit'],'tables':['public.报表项层级']}}
    assert _definition_document(doc, hierarchy)
    assert not _definition_document(doc, product)


def test_record_models_keeps_only_this_tables_definition(tmp_path):
    table,_,model=fixture()
    with GraphStore(tmp_path/'knowledge.sqlite') as store:
        ledger=setup_ledger(store,[table])
        body=packet(table)
        body['schema_knowledge']=ledger.context(table)
        body['document_snippets']=[
            {'doc_id':'paths','revision':'1','section_id':'guide',
             'scope':{'tables':['public.paths'],'source_ids':['s']}},
            {'doc_id':'product','revision':'1','section_id':'guide',
             'scope':{'tables':['public.产品定义'],'source_ids':['s']}}]
        from lightgraph.discovery.proposal_session import ProposalSession
        session=ProposalSession(body)
        assert session.execute('declare_object_model',model)['status']=='ok'
        ledger.record_models(table,session.batch(abstain=True)._runtime_object_models,body)
        assert ledger._load(table)['document_refs']==['paths@1:guide']


def test_executed_rule_retires_the_agent_candidate_and_stops_fallback(tmp_path):
    table,_,model=fixture()
    model={**model,'position_semantics':'ordinal_slots'}
    with GraphStore(tmp_path/'knowledge.sqlite') as store:
        ledger=setup_ledger(store,[table])
        body=packet(table)
        body['schema_knowledge']=ledger.context(table)
        from lightgraph.discovery.proposal_session import ProposalSession
        session=ProposalSession(body)
        assert session.execute('declare_object_model',model)['status']=='ok'
        ledger.record_models(table,session.batch(abstain=True)._runtime_object_models,body)
        rule=RuleSpec(rule_id='hierarchy',kind='wide_hierarchy',source={'source_id':'s','table':'paths'},
            scope={'namespaces':['tenant_id']},
            identity={'key_columns':['tenant_id','item_l1_code'],'entity_kind':'ReportItem'},
            hierarchy={'levels':[{'level':level,'code':f'item_l{level}_code','label':f'item_l{level}_cn_name'}
                for level in range(1,6)],'taxonomy':'paths','order_evidence':[DEFINITION]})
        ledger.record_rule(rule)
        visible=ledger.current_objects(table)
        assert not any(obj.get('basis')=='agent_field_model' for obj in visible)
        assert proposals_from_field_models(visible,[DEFINITION])==[]
