"""Bind each ephemeral model-visible cell to its header; never retain records.

Physical/probe protocols stay unchanged. This presentation copy is used only at
the model boundary, so tuple validation and authorization keep their typed APIs.
"""
from copy import deepcopy


def named_rows(columns, rows):
    if not isinstance(columns,list) or not columns or not all(isinstance(c,str) for c in columns):
        return rows, {}
    if not isinstance(rows,list) or any(not isinstance(row,list) or len(row)!=len(columns) for row in rows):
        return rows, {}
    # Duplicate SQL aliases must never overwrite a cell in a dictionary.
    keys=columns if len(set(columns))==len(columns) else [f'field_{i+1}' for i in range(len(columns))]
    return [dict(zip(keys,row)) for row in rows], {'row_format':'named_fields',
        'column_bindings':[{'key':key,'name':name,'position':i} for i,(key,name) in enumerate(zip(keys,columns))]}


def present_tool_result(result):
    if not isinstance(result,dict) or not isinstance(result.get('data'),dict):return result
    data=result['data']
    rows,bindings=named_rows(data.get('columns'),data.get('rows'))
    if not bindings:return result
    return {**result,'data':{**data,'rows':rows,**bindings}}


def present_observations(packet):
    """Keep IDs plus readable names adjacent to each initial sample page."""
    result=dict(packet)
    if packet.get('tool_observations'):
        result['tool_observations']=[]
        for observation in packet['tool_observations']:
            rows,bindings=named_rows(observation.get('columns'),observation.get('rows'))
            result['tool_observations'].append({**observation,'rows':rows,**bindings})
    if packet.get('strata_samples'):
        columns=[{k:c[k] for k in ('id','name','type') if k in c}
                 for c in packet.get('metadata_digest',{}).get('columns',[])]
        result['strata_samples']=[{**sample,'columns':deepcopy(columns),
            'row_format':'column_id_fields','source':packet.get('scope',{})}
            for sample in packet['strata_samples']]
    return result
