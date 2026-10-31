"""Model rows are self describing without changing probe tuples or persistence."""
from copy import deepcopy

from lightgraph.discovery.table_presentation import present_observations, present_tool_result


def test_named_tool_rows_keep_types_correlations_and_completeness():
    raw={'status':'ok','probe_id':'p1','data':{'columns':['code','name','amount'],
        'rows':[[0,'零',None],['001','甲',2.5]],'complete':False,'row_count':2}}
    before=deepcopy(raw)
    visible=present_tool_result(raw)
    assert visible['data']['rows']==[{'code':0,'name':'零','amount':None},
                                    {'code':'001','name':'甲','amount':2.5}]
    assert visible['data']['complete'] is False
    assert visible['probe_id']=='p1' and raw==before


def test_duplicate_sql_aliases_keep_both_cells_and_their_headers():
    visible=present_tool_result({'data':{'columns':['code','code'],'rows':[['A','B']]}})['data']
    assert visible['rows']==[{'field_1':'A','field_2':'B'}]
    assert visible['column_bindings']==[
        {'key':'field_1','name':'code','position':0},
        {'key':'field_2','name':'code','position':1}]


def test_initial_samples_and_review_observations_carry_names_and_source():
    raw={'scope':{'source_id':'s','table':'t'},
         'metadata_digest':{'columns':[{'id':'c1','name':'编码','type':'text'}]},
         'strata_samples':[{'rows':[{'c1':'X'}]}],
         'tool_observations':[{'columns':['编码'],'rows':[['X']],'complete':True}]}
    before=deepcopy(raw)
    visible=present_observations(raw)
    assert visible['strata_samples'][0]['columns']==raw['metadata_digest']['columns']
    assert visible['strata_samples'][0]['source']==raw['scope']
    assert visible['tool_observations'][0]['rows']==[{'编码':'X'}]
    assert visible['tool_observations'][0]['complete'] is True
    assert raw==before
