"""Compiler-owned nested counts reach review; arbitrary nested observations do not."""
import json

from lightgraph.contracts import ColumnSpec, ExtractionGoal, TableSpec
from lightgraph.discovery import BudgetAllocator
from lightgraph.discovery.agent import _record_probe
from lightgraph.discovery.engine import _column_id
from lightgraph.discovery.models import TaskPacket
from lightgraph.discovery.semantics import review_semantics
from lightgraph.discovery.tools import ToolExecutor
from lightgraph.sources import MemorySource


def test_profile_and_fd_counts_reach_independent_reviewer_without_source_values():
    table=TableSpec(source_id='s',name='items',columns=[ColumnSpec(name=c) for c in ('code','label')])
    packet={'task':'T0_discovery','scope':{'source_id':'s','table':'items'},
        'metadata_digest':{'columns':[{'id':_column_id(table,c.name),'name':c.name} for c in table.columns]},
        'probe_digest':[]}
    with MemorySource([table],{'public.items':[('A','PRIVATE_ONE'),('A','PRIVATE_TWO')]}) as source:
        tools=ToolExecutor(source,ExtractionGoal(),BudgetAllocator(),allow_source_samples=True)
        for name,args in [('profile_columns',{'table':'public.items','columns':['code']}),
                          ('check_fd',{'table':'public.items','keys':['code'],'values':['label']})]:
            result=tools.execute(name,args)
            assert result['status']=='ok'
            _record_probe(packet,result,name)
        tools.close()
    checked=TaskPacket.model_validate(packet).model_dump(mode='json',by_alias=True)
    class Model:
        def complete(self,task,request,schema):
            first,second=request['probe_digest']
            assert first['aggregates'][_column_id(table,'code')]['rows']==2
            assert first['aggregates'][_column_id(table,'code')]['distinct_exact']==1
            assert second['aggregates'][_column_id(table,'label')]['violating_groups']==1
            assert 'PRIVATE_' not in json.dumps(request)
            return schema.model_validate({'proposition_ref':request['semantic_proposition_ref'],
                'decision':'supported','basis':'data_inference',
                'evidence':[{'reference':second['probe_id'],'stance':'supports'}]})
    assert review_semantics(Model(),checked,{'kind':'test'},[])


def test_nested_probe_summary_rejects_unissued_columns_and_source_fields():
    packet={'metadata_digest':{'columns':[{'id':'c_registered','name':'label'}]},'probe_digest':[]}
    result={'status':'ok','probe_id':'probe_0001','aggregate_columns':{
        'label':'c_registered','PRIVATE_COLUMN':'c_forged'},'aggregates':{
        'label':{'rows':2,'values':'PRIVATE_VALUE','other':{'rows':3},'max_len':float('inf')},
        'PRIVATE_COLUMN':{'rows':4}}}
    _record_probe(packet,result,'profile_columns')
    assert packet['probe_digest'][0]['aggregates']=={'c_registered':{'rows':2}}
    assert 'PRIVATE_' not in json.dumps(packet)
