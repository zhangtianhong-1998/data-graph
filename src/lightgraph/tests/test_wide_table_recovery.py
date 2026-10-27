"""Realistic provider-shaped responses on a five-level, 16-column wide table.

The provider is offline: these cases prove recovery and graph materialization,
not the accuracy of a real model on customer data.
"""
from copy import deepcopy

from lightgraph.contracts import ColumnSpec, ExtractionGoal, TableSpec
from lightgraph.pipeline import run_pipeline
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import client, input_packet, response


def wide_fixture():
    names = [f'L{i}_{part}' for i in range(1, 6) for part in ('CODE', 'CN_NAME', 'EN_NAME')]
    names.append('RESPONSIBILITY_CENTER')
    table = TableSpec(source_id='s', name='report_paths',
        comment='Each row is an ordered parent-child path from L1 to L5. The center is assigned the L5 item.',
        columns=[ColumnSpec(name=name) for name in names])
    def row(suffix, center):
        codes = ['A', 'B', 'C', 'D'+suffix, 'E'+suffix]
        return tuple(v for code in codes for v in (code, '项目'+code, 'Item '+code)) + (center,)
    first = row('1', 'CENTER_A')
    return table, {'public.report_paths': [first, row('2', 'CENTER_B'), first]}


def proposals(packet, *, missing_definition=False):
    ids = {c['name']: c['id'] for c in packet['metadata_digest']['columns']}
    hierarchy = {'kind':'wide_hierarchy', 'claim':{
        'group_id':'path', 'entity_kind_hint':'ReportItem',
        'levels':[{'position':i, 'key':ids[f'L{i}_CODE'], 'label':ids[f'L{i}_CN_NAME'],
                   'alias':ids[f'L{i}_EN_NAME']} for i in range(1, 6)],
        'ordering_evidence':[ids['L1_CODE'], ids['L2_CODE']]}, 'evidence_cited':[ids['L1_CODE']]}
    center = {'kind':'column_group', 'claim':{'group_id':'center', 'entity_kind_hint':'Dimension',
        'members':[{'column_id':ids['RESPONSIBILITY_CENTER'], 'role':'identity_code', 'in_identity':True},
                   {'column_id':ids['RESPONSIBILITY_CENTER'], 'role':'semantic_attribute',
                    'property_name':'center_category'}]}, 'evidence_cited':[ids['RESPONSIBILITY_CENTER']]}
    schema = {'properties':[{'name':'label', 'description':'Display name of a report item.',
                              'entity_kinds':['ReportItem'], 'value_type':'string'}]}
    if not missing_definition:
        schema['properties'].append({'name':'center_category', 'description':'Responsible center classification.',
            'entity_kinds':['Dimension'], 'value_type':'string'})
    return {'task':packet['task'], 'semantic_schema':schema, 'proposals':[hierarchy, center]}


def test_repair_wide_table_then_discover_hierarchy_to_center_parallel_relations(monkeypatch, tmp_path):
    table, rows = wide_fixture()
    calls=[]
    def handler(request):
        payload, packet = input_packet(request)
        calls.append(packet['task'])
        if packet['task']=='T0_discovery':
            if len(payload['messages'])==2:
                return response(call={'sql':'SELECT '+', '.join(table.column_names)+' FROM public.report_paths', 'max_rows':3})
            return response(proposals(packet, missing_definition=True))
        if packet['task']=='T5_revision':
            fixed=proposals(packet)
            # A correction for one group need not resubmit its valid sibling.
            fixed['proposals']=fixed['proposals'][1:]
            return response(fixed)
        if packet['task']=='semantic_review':
            if packet['semantic_proposition'].get('kind')=='wide_hierarchy' and 'semantic_review_repair' not in packet:
                echo=deepcopy(packet['semantic_proposition'])
                echo['claim'].pop('gap_policy')
                return response({'proposition':echo, 'decision':'supported', 'basis':'data_inference',
                    'evidence':[{'reference':packet['metadata_digest']['columns'][0]['id'], 'stance':'supports'}]})
            return response({'proposition_ref':packet['semantic_proposition_ref'], 'decision':'supported',
                'basis':'data_inference', 'evidence':[{
                    'reference':packet['metadata_digest']['columns'][0]['id'], 'stance':'supports'}]})
        if packet['task']=='T2_cross_table':
            ids={c['name']:c['id'] for c in packet['metadata_digest']['columns']}
            hierarchy=next(b for b in packet['entity_bindings'] if b.get('level')==5)
            center=next(b for b in packet['entity_bindings'] if b['entity_kind']=='Dimension')
            relations=['assigned_to','classified_by']
            return response({'task':packet['task'], 'semantic_schema':{'relations':[
                {'name':name,'description':'An explicit business link supported by this table interpretation.',
                 'source_kinds':['ReportItem'],'target_kinds':['Dimension']} for name in relations]},
                'proposals':[{'kind':'reference','claim':{
                    'source_entity':hierarchy['rule_id'], 'source_level':5,
                    'target_entity':center['rule_id'], 'source_columns':[ids['L5_CODE']],
                    'target_columns':[ids['RESPONSIBILITY_CENTER']], 'relation_kind':name,
                    'cardinality_hint':'n_to_1'},'evidence_cited':[ids['L5_CODE']]} for name in relations]})
        return response({'task':packet['task'],'abstain':True})
    config=RunConfig(sources=[],output_dir=str(tmp_path/'out'),max_identity_reviews=0,
        goal=ExtractionGoal(purpose='发现报表项目层级和责任中心关系'))
    with MemorySource([table],rows) as source:
        report=run_pipeline(config,[source],model=client(monkeypatch,handler))
    assert report.status=='COMPLETED',report.model_dump()
    assert len(report.rules_published)==4 and not report.rules_skipped
    assert report.counts['business_nodes']==9 and report.counts['business_edges']==10
    assert report.validation['business_graph']=='entities_and_relations'
    assert report.counts['recovered_proposals']>=1 and 'T5_revision' in calls
    with GraphStore(tmp_path/'out/graph.sqlite') as store:
        graph=store.view({'public'})
        edges=[e for e in graph['edges'] if e['relation'] in ('parent_of','assigned_to','classified_by')]
        assert sum(e['relation']=='parent_of' for e in edges)==6
        assert sum(e['relation']=='assigned_to' for e in edges)==2
        assert sum(e['relation']=='classified_by' for e in edges)==2
        references=[r for r in store.get_rules() if r.kind=='reference']
        assert len({r.rule_id for r in references})==2
        assert all(r.relation.source_level==5 for r in references)
        assert any('Item E1' in n['aliases'] for n in graph['nodes'] if n['layer']=='business')
    before=len(calls)
    with MemorySource([table],rows) as source:
        reused=run_pipeline(config,[source],model=client(monkeypatch,handler))
    assert len(calls)==before and reused.counts['business_edges']==10


def test_rejected_hierarchy_does_not_hide_independent_entities(monkeypatch,tmp_path):
    table, rows=wide_fixture()
    def handler(request):
        _,packet=input_packet(request)
        if packet['task']=='T0_discovery':return response(proposals(packet))
        if packet['task']=='semantic_review':
            return response({'proposition_ref':packet['semantic_proposition_ref'],'decision':'refuted',
                'basis':'data_inference','evidence':[{'reference':packet['metadata_digest']['columns'][0]['id'],
                                                     'stance':'refutes'}]})
        return response({'task':packet['task'],'abstain':True})
    with MemorySource([table],rows) as source:
        report=run_pipeline(RunConfig(sources=[],output_dir=str(tmp_path/'out')),[source],model=client(monkeypatch,handler))
    assert report.counts['business_nodes']==2 and report.counts['business_edges']==0
    assert report.validation['business_graph']=='entities_only'
    assert any(i['code']=='semantic_claim_refuted' for i in report.issues)


def test_no_business_graph_is_reported_separately_from_metadata(monkeypatch,tmp_path):
    table, rows=wide_fixture()
    def handler(request):
        _,packet=input_packet(request)
        return response({'task':packet['task'],'abstain':True})
    with MemorySource([table],rows) as source:
        report=run_pipeline(RunConfig(sources=[],output_dir=str(tmp_path/'out')),[source],model=client(monkeypatch,handler))
    assert report.counts['business_nodes']==report.counts['business_edges']==0
    assert report.validation['business_graph']=='empty'
    with GraphStore(tmp_path/'out/graph.sqlite') as store:
        assert store.view({'public'})['nodes']  # Metadata is available, business output is empty.


def test_unrepaired_contract_is_not_cached_as_completed_and_next_run_recovers(monkeypatch,tmp_path):
    from lightgraph.tests.test_agent_pipeline import group
    table=TableSpec(source_id='s',name='catalog',columns=[ColumnSpec(name=c) for c in ('code','title','unit_name')])
    corrected=False;calls=[]
    def handler(request):
        _,packet=input_packet(request);calls.append(packet['task'])
        proposal=group(packet,'code','title',extra=[
            ('unit_name',{'role':'semantic_attribute','property_name':'display_unit'})])
        if corrected:
            proposal['semantic_schema']={'properties':[{'name':'display_unit','description':'Display unit from the catalog',
                'entity_kinds':['ReportItem'],'value_type':'string'}]}
        return response(proposal)
    config=RunConfig(sources=[],output_dir=str(tmp_path/'out'))
    rows={'public.catalog':[('K','A reusable object','items')]}
    with MemorySource([table],rows) as source:
        first=run_pipeline(config,[source],model=client(monkeypatch,handler))
    assert first.counts['business_nodes']==0 and first.counts['work_deferred']>=1
    assert first.status=='PARTIAL'
    assert any(i['code']=='unregistered_attribute_semantics' for i in first.issues)
    with GraphStore(tmp_path/'out/graph.sqlite') as store:
        from lightgraph.pipeline import _context_versions
        assert not _context_versions(store.current_versions('discovery:s') or {})
    before=len(calls);corrected=True
    with MemorySource([table],rows) as source:
        second=run_pipeline(config,[source],model=client(monkeypatch,handler))
    assert len(calls)>before and second.counts['business_nodes']==1
    assert second.counts['work_deferred']==0 and second.status=='COMPLETED'
    before=len(calls)
    with MemorySource([table],rows) as source:
        third=run_pipeline(config,[source],model=client(monkeypatch,handler))
    assert len(calls)==before and third.counts['business_nodes']==1
