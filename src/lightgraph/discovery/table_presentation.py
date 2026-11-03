"""Compact, typed tables at the model boundary; never retain records.

Physical/probe protocols stay unchanged. This presentation copy is used only at
the model boundary, so tuple validation and authorization keep their typed APIs.
"""
from copy import deepcopy


TABLE_READING = ('Tables with row_format=positional_values have one ordered columns header. '
    'Each rows[i][j] belongs to columns[j]; positions are zero-based, including duplicate names. '
    'Strings, numbers, booleans and null keep their JSON types. omitted_cells maps row indexes '
    'to column positions withheld from that row: their null placeholders are NOT observed nulls. '
    'Respect sample/complete/coverage markers; a displayed page is not necessarily the full data.')


def tabular_rows(columns, rows):
    if not isinstance(columns,list) or not columns or not all(isinstance(c,str) for c in columns):
        return rows, {}
    if not isinstance(rows,list) or any(not isinstance(row,list) or len(row)!=len(columns) for row in rows):
        return rows, {}
    # A single ordered header avoids repeating field names in every record.
    # Positional cells also preserve duplicate SQL aliases without collisions.
    return deepcopy(rows), {'row_format':'positional_values'}


def present_tool_result(result):
    if not isinstance(result,dict) or not isinstance(result.get('data'),dict):return result
    data=result['data']
    rows,bindings=tabular_rows(data.get('columns'),data.get('rows'))
    if not bindings:return result
    return {**result,'data':{**data,'rows':rows,**bindings}}


def present_observations(packet):
    """Keep IDs plus readable names adjacent to each initial sample page."""
    result=dict(packet)
    if packet.get('tool_observations'):
        result['tool_observations']=[]
        for observation in packet['tool_observations']:
            rows,bindings=tabular_rows(observation.get('columns'),observation.get('rows'))
            result['tool_observations'].append({**observation,'rows':rows,**bindings})
    if packet.get('strata_samples'):
        columns=[{k:c[k] for k in ('id','name','type') if k in c}
                 for c in packet.get('metadata_digest',{}).get('columns',[])]
        result['strata_samples']=[]
        for sample in packet['strata_samples']:
            rows=sample.get('rows')
            shown={**sample,'columns':deepcopy(columns),'source':packet.get('scope',{})}
            ids=[c.get('id') for c in columns]
            if (ids and len(ids)==len(set(ids)) and None not in ids and isinstance(rows,list)
                    and all(isinstance(row,dict) and not set(row)-set(ids) for row in rows)):
                omitted={str(i):[j for j,key in enumerate(ids) if key not in row]
                         for i,row in enumerate(rows) if len(row)!=len(ids)}
                shown.update(rows=[[row.get(key) for key in ids] for row in rows],
                             row_format='positional_values')
                if omitted:shown['omitted_cells']=omitted
            else:
                # Never drop unrecognised fields or coerce malformed observations.
                shown['row_format']='column_id_fields'
            result['strata_samples'].append(shown)
    return result
