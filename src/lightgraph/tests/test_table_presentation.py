"""Model rows are self describing without changing probe tuples or persistence."""
from copy import deepcopy

from lightgraph.discovery.table_presentation import present_observations, present_tool_result


def test_named_tool_rows_keep_types_correlations_and_completeness():
    raw={'status':'ok','probe_id':'p1','data':{'columns':['code','name','amount'],
        'rows':[[0,'零',None],['001','甲',2.5]],'complete':False,'row_count':2}}
    before=deepcopy(raw)
    visible=present_tool_result(raw)
    assert visible['data']['rows']==raw['data']['rows']
    assert visible['data']['row_format']=='positional_values'
    assert visible['data']['columns']==['code','name','amount']
    assert visible['data']['complete'] is False
    assert visible['probe_id']=='p1' and raw==before


def test_duplicate_sql_aliases_keep_both_cells_and_their_headers():
    visible=present_tool_result({'data':{'columns':['code','code'],'rows':[['A','B']]}})['data']
    assert visible['rows']==[['A','B']]
    assert visible['columns']==['code','code']
    assert visible['row_format']=='positional_values'


def test_initial_samples_and_review_observations_carry_names_and_source():
    raw={'scope':{'source_id':'s','table':'t'},
         'metadata_digest':{'columns':[{'id':'c1','name':'编码','type':'text'}]},
         'strata_samples':[{'rows':[{'c1':'X'}]}],
         'tool_observations':[{'columns':['编码'],'rows':[['X']],'complete':True}]}
    before=deepcopy(raw)
    visible=present_observations(raw)
    assert visible['strata_samples'][0]['columns']==raw['metadata_digest']['columns']
    assert visible['strata_samples'][0]['source']==raw['scope']
    assert visible['strata_samples'][0]['rows']==[['X']]
    assert visible['tool_observations'][0]['rows']==[['X']]
    assert visible['tool_observations'][0]['complete'] is True
    assert raw==before


def test_sparse_cells_are_distinct_from_actual_nulls_and_unrecognised_fields_survive():
    packet={'metadata_digest':{'columns':[{'id':'a','name':'编码'},{'id':'b','name':'名称'}]},
        'strata_samples':[{'rows':[{'a':'001','b':None},{'a':'002'}]},
                          {'rows':[{'a':'003','unknown':'must retain'}]}]}
    shown=present_observations(packet)['strata_samples']
    assert shown[0]['rows']==[['001',None],['002',None]]
    assert shown[0]['omitted_cells']=={'1':[1]}
    assert shown[1]['row_format']=='column_id_fields'
    assert shown[1]['rows']==packet['strata_samples'][1]['rows']


def test_compact_tables_preserve_all_cells_and_reduce_repeated_field_tokens():
    from lightgraph.contracts import canonical
    from lightgraph.discovery.token_estimation import estimate_tokens
    columns=[f'REPORT_ITEM_L{i}_{role}' for i in range(1,6) for role in ('CODE','CN_NAME','EN_NAME')]
    row=['001','名称含逗号,换行\n和引号"','English name']*5
    raw={'data':{'columns':columns,'rows':[row[:] for _ in range(100)],'complete':False}}
    presented=present_tool_result(raw)
    previous={'data':{**raw['data'],'rows':[dict(zip(columns,r)) for r in raw['data']['rows']]}}
    assert presented['data']['rows']==raw['data']['rows']
    assert estimate_tokens(canonical(presented))<estimate_tokens(canonical(previous))*.6
    assert len(canonical(presented))<len(canonical(previous))*.6


def test_short_reference_translation_changes_headers_never_cell_values():
    from lightgraph.discovery.model_references import ModelReferences
    column='c_0123456789012345'
    packet={'metadata_digest':{'columns':[{'id':column,'name':'编码'}]},
        'strata_samples':[{'rows':[{column:column},{column:'col1'}]}]}
    shown=ModelReferences(packet).encode(present_observations(packet))
    assert shown['strata_samples'][0]['columns'][0]['id']=='col1'
    assert shown['strata_samples'][0]['rows']==[[column],['col1']]
