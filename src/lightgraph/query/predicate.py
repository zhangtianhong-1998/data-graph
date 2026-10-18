"""Predicate/1.0: bounded typed trees, bound values, and explicit NULL semantics."""
from __future__ import annotations
from datetime import date, datetime
from dataclasses import dataclass, field
import math
from lightgraph.errors import LightGraphError

MAX_DEPTH=3
MAX_NODES=32
MAX_VALUES=128
TEXT_TYPES={'text','varchar','character varying','character','char','nvarchar','string'}
INTEGER_TYPES={'integer','int','int2','int4','int8','smallint','bigint'}
NUMERIC_TYPES=INTEGER_TYPES|{'numeric','decimal','real','double precision','float','float4','float8'}
DATE_TYPES={'date','timestamp','timestamp without time zone','timestamp with time zone','timestamptz'}
ALIASES={'and':'all','gte':'ge','lte':'le','not_null':'is_not_null'}


@dataclass(frozen=True, repr=False)
class ScopeConjunction:
    """Internal scope plus partition filter, never part of the user JSON DSL."""
    user_predicate: dict | None = field(repr=False)
    partition_filter: dict = field(repr=False)

    def __repr__(self): return "ScopeConjunction()"
    def __reduce_ex__(self, protocol): raise LightGraphError("ephemeral_serialization_forbidden")


def combine_scope_predicates(user_predicate, partition_filter):
    if user_predicate is not None and not isinstance(user_predicate, dict):
        raise LightGraphError("invalid_predicate")
    if not isinstance(partition_filter, dict) or not partition_filter:
        raise LightGraphError("invalid_predicate")
    return ScopeConjunction(user_predicate, partition_filter)


def compile_predicate(table,predicate,params,*,column_sql,dialect):
    if isinstance(predicate, ScopeConjunction):
        # Each user/generated tree keeps the original depth and node bound;
        # shared bindings retain a single total value budget for the SQL query.
        left = compile_predicate(table,predicate.user_predicate,params,column_sql=column_sql,dialect=dialect)
        right = compile_predicate(table,predicate.partition_filter,params,column_sql=column_sql,dialect=dialect)
        return f'({left}) AND ({right})'
    if predicate is None or predicate=={}:return '1 = 1'
    nodes=0
    def bind(value):
        if len(params)>=MAX_VALUES:raise LightGraphError('predicate_values_limit')
        key=f'p{len(params)}';params[key]=value;return ':'+key
    def scalar(value,datatype):
        if value is None:raise LightGraphError('predicate_null_requires_explicit_operator')
        if type(value) not in (str,int,float,bool):raise LightGraphError('predicate_value_type_mismatch')
        if isinstance(value,str) and len(value)>4096:raise LightGraphError('predicate_value_size_limit')
        if isinstance(value,float) and not math.isfinite(value):raise LightGraphError('non_finite_predicate_value')
        if datatype in TEXT_TYPES:
            if type(value) is not str:raise LightGraphError('predicate_value_type_mismatch')
            return value
        if datatype in NUMERIC_TYPES:
            if type(value) not in (int,float) or datatype in INTEGER_TYPES and type(value) is not int:
                raise LightGraphError('predicate_value_type_mismatch')
            return value
        if datatype in {'boolean','bool'}:
            if type(value) is not bool:raise LightGraphError('predicate_value_type_mismatch')
            return value
        if datatype in DATE_TYPES:
            if type(value) is not str:raise LightGraphError('predicate_value_type_mismatch')
            try:parsed=date.fromisoformat(value) if datatype=='date' else datetime.fromisoformat(value)
            except (ValueError,TypeError):raise LightGraphError('predicate_date_invalid') from None
            return parsed if dialect=='postgres' else parsed.isoformat()
        raise LightGraphError('predicate_type_unsupported')
    def visit(node,depth):
        nonlocal nodes
        nodes+=1
        if depth>MAX_DEPTH:raise LightGraphError('predicate_depth_limit')
        if nodes>MAX_NODES:raise LightGraphError('predicate_nodes_limit')
        if not isinstance(node,dict) or not node:raise LightGraphError('invalid_predicate')
        if set(node)&{'all','any','not','and'}:
            if len(node)!=1:raise LightGraphError('invalid_predicate')
            op=next(iter(node));value=node[op];op=ALIASES.get(op,op)
            if op=='not':return '(NOT ('+visit(value,depth+1)+'))'
            if not isinstance(value,list) or not 1<=len(value)<=16:raise LightGraphError('predicate_children_limit')
            return '('+(' AND ' if op=='all' else ' OR ').join(visit(c,depth+1) for c in value)+')'
        if 'column' not in node:
            # Compatibility with {tenant: "A"} and {code: {op: in, value: [...]}}.
            normalized=[]
            for column,condition in node.items():
                if isinstance(condition,dict):
                    if set(condition)-{'op','value','values'}:raise LightGraphError('invalid_predicate')
                    item={'column':column,'op':condition.get('op','eq'),**condition}
                    if item['op'] in ('in','not_in','between') and 'value' in item and 'values' not in item:
                        item['values']=item.pop('value')
                else:item={'column':column,'op':'eq','value':condition}
                normalized.append(item)
            # Normalization is not another logical level for a single leaf.
            nodes-=1
            return visit(normalized[0] if len(normalized)==1 else {'all':normalized},depth)
        if 'op' not in node or type(node['column']) is not str or type(node['op']) is not str:
            raise LightGraphError('invalid_predicate')
        column=node['column'];op=ALIASES.get(node['op'],node['op'])
        c=column_sql(column)
        declared=next(col.data_type for col in table.columns if col.name==column)
        datatype=declared.lower().split('(',1)[0].strip()
        if datatype=='mixed':raise LightGraphError('predicate_type_unsupported')
        if op in ('is_null','is_not_null'):
            if set(node)!={'column','op'}:raise LightGraphError('invalid_predicate')
            return '('+c+(' IS NULL)' if op=='is_null' else ' IS NOT NULL)')
        if op in ('in','not_in','between'):
            if set(node)!={'column','op','values'}:raise LightGraphError('invalid_predicate')
            values=node['values']
            if not isinstance(values,list) or not 1<=len(values)<=64:raise LightGraphError('predicate_values_limit')
            if any(type(v) is not type(values[0]) for v in values):raise LightGraphError('predicate_value_type_mismatch')
            converted=[scalar(v,datatype) for v in values]
            if op=='between':
                if len(values)!=2 or datatype not in NUMERIC_TYPES|DATE_TYPES:
                    raise LightGraphError('predicate_between_invalid')
                try:ordered=converted[0]<=converted[1]
                except TypeError:raise LightGraphError('predicate_between_invalid') from None
                if not ordered:raise LightGraphError('predicate_between_invalid')
                return f'({c} BETWEEN {bind(converted[0])} AND {bind(converted[1])})'
            return '('+c+(' NOT IN (' if op=='not_in' else ' IN (')+', '.join(bind(v) for v in converted)+'))'
        if op not in {'eq','ne','gt','ge','lt','le','prefix'} or set(node)!={'column','op','value'}:
            raise LightGraphError('invalid_predicate')
        value=scalar(node['value'],datatype)
        if op=='prefix':
            if datatype not in TEXT_TYPES:raise LightGraphError('predicate_prefix_requires_text')
            escaped=value.replace('!','!!').replace('%','!%').replace('_','!_')+'%'
            return f"({c} LIKE {bind(escaped)} ESCAPE '!')"
        symbol={'eq':'=','ne':'<>','gt':'>','ge':'>=','lt':'<','le':'<='}[op]
        return f'({c} {symbol} {bind(value)})'
    return visit(predicate,1)
