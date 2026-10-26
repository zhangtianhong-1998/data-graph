"""Goal-open vocabulary through a real pipeline with an offline Agent provider."""
from lightgraph.contracts import ColumnSpec, TableSpec, ForeignKeySpec, ExtractionGoal
from lightgraph.pipeline import run_pipeline
from lightgraph.runtime.config import RunConfig
from lightgraph.runtime.retention import retain_check
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import client, input_packet, response, group


def test_agent_defines_people_organizations_and_materializes_many_to_many(monkeypatch,tmp_path):
    types=[{'name':'Person','description':'A person represented in the authorized personnel directory.'},
           {'name':'Organization','description':'An organization named in the authorized employer directory.'}]
    properties=[{'name':'birth_year','description':'Calendar year of birth from the directory.',
                 'value_type':'integer','entity_kinds':['Person'],'multi_valued':False}]
    relation={'name':'employed_by','description':'Employment of a person by an organization.',
              'source_kinds':['Person'],'target_kinds':['Organization']}
    people=TableSpec(source_id='s',name='people',columns=[ColumnSpec(name=c) for c in
        ('person_key','person_title','year_field','observation_total')])
    organizations=TableSpec(source_id='s',name='organizations',columns=[ColumnSpec(name=c) for c in
        ('org_key','org_title')])
    membership=TableSpec(source_id='s',name='membership',
        comment='Each row records employment of a person by an organization; multiple employers are allowed.',
        columns=[ColumnSpec(name=c) for c in ('member_ref','org_ref')],
        foreign_keys=[ForeignKeySpec(name='person_link',columns=['member_ref'],target_schema='public',
            target_table='people',target_columns=['person_key']),
            ForeignKeySpec(name='org_link',columns=['org_ref'],target_schema='public',
            target_table='organizations',target_columns=['org_key'])])
    marker='EXCLUDED_OBSERVATION_881299'
    rows={'public.people':[('P1','甲',1980,marker),('P2','乙',1990,marker)],
          'public.organizations':[('O1','组织一'),('O2','组织二')],
          'public.membership':[('P1','O1'),('P1','O2'),('P2','O1'),('P1','O1')]}
    stages=[]
    def handler(request):
        _,p=input_packet(request);stages.append(p['task']);name=p['scope']['table']
        assert p['goal']['purpose']=='发现人员、组织及雇佣关系，只输出目录知识'
        assert p['goal']['entity_kinds']==p['goal']['relation_kinds']==[]
        if p['task']=='semantic_review':
            assert not p['document_snippets']
            link=next(t for t in p['catalog'] if t['name']=='membership')
            assert len(link['foreign_keys'])==2 and 'employment' in link['comment']
            assert p['semantic_proposition']['claim']['relation_kind']=='employed_by'
            assert any(probe['probe_id']=='backend_endpoint_verification' for probe in p['probe_digest'])
            return response({'proposition':p['semantic_proposition'],'decision':'supported',
                'basis':'data_inference','evidence':[{'reference':'backend_endpoint_verification','stance':'supports'}]})
        if p['task']=='T0_discovery' and name=='people':
            assert 'observation_total' not in {c['name'] for c in p['metadata_digest']['columns']}
            proposal=group(p,'person_key','person_title','Person',extra=[
                ('year_field',{'role':'semantic_attribute','property_name':'birth_year'})])
            proposal['semantic_schema']={'entity_types':types,'properties':properties}
            return response(proposal)
        if p['task']=='T0_discovery' and name=='organizations':
            proposal=group(p,'org_key','org_title','Organization')
            proposal['semantic_schema']={'entity_types':types}
            return response(proposal)
        if p['task']=='T2_cross_table' and name=='membership':
            bindings={b['entity_kind']:b['rule_id'] for b in p['entity_bindings']}
            ids={c['name']:c['id'] for c in p['metadata_digest']['columns']}
            assert {'Person','Organization'} <= {t['name'] for t in p['semantic_schema']['entity_types']}
            return response({'task':p['task'],'semantic_schema':{'relations':[relation]},
                'proposals':[{'kind':'reference','claim':{
                    'source_entity':bindings['Person'],'target_entity':bindings['Organization'],
                    'source_columns':[ids['member_ref']],'target_columns':[ids['org_ref']],
                    'relation_kind':'employed_by','cardinality_hint':'n_to_m'},'evidence_cited':[ids['member_ref']]}]})
        return response({'task':p['task'],'abstain':True})
    goal=ExtractionGoal(purpose='发现人员、组织及雇佣关系，只输出目录知识',
        excluded_columns=['observation_total'])
    cfg=RunConfig(sources=[],output_dir=str(tmp_path/'out'),goal=goal)
    with MemorySource([people,organizations,membership],rows) as source:
        report=run_pipeline(cfg,[source],model=client(monkeypatch,handler))
    assert not report.rules_skipped,report.issues
    with GraphStore(tmp_path/'out/graph.sqlite') as store:
        graph=store.view({'public'})
        assert len([n for n in graph['nodes'] if n['kind']=='Person'])==2
        assert len([n for n in graph['nodes'] if n['kind']=='Organization'])==2
        assert {n['properties']['birth_year'] for n in graph['nodes'] if n['kind']=='Person'}=={1980,1990}
        edges=[e for e in graph['edges'] if e['relation']=='employed_by']
        assert len(edges)==3  # two employers and two employees; no pairwise Cartesian expansion
        rules=[r for r in store.get_rules() if r.kind=='reference']
        assert len(rules)==1 and rules[0].evidence.semantic_decision=='model_review'
        assert rules[0].relation.source_columns==['member_ref']
        assert rules[0].relation.target_columns==['org_ref']
        assert any(s['rule_id']==rules[0].rule_id for s in graph['supports'])
    assert 'semantic_review' in stages
    assert retain_check([tmp_path/'out'],[marker])['violations']==0
