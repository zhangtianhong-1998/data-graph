"""Machine-readable snapshot schema, derived from the persistence registry."""
from ..store.policy import PROPERTY_NAMES,QUALIFIER_NAMES,KINDS,RELATION_STATES


def snapshot_schema():
    scalar={'type':['string','number','boolean','null']}
    qualifiers={'type':'object','propertyNames':{'enum':sorted(QUALIFIER_NAMES)},'additionalProperties':scalar}
    def obj(fields,required=None):return {'type':'object','properties':fields,'required':required or list(fields),'additionalProperties':False}
    text={'type':'string'}
    payloads={
        'NodeExistence':obj({'entity_id':text,'kind':{'enum':sorted(set().union(*KINDS.values()))},'layer':{'enum':list(KINDS)}}),
        'PropertyValue':obj({'entity_id':text,'name':{'enum':sorted(PROPERTY_NAMES)},'value':scalar,'qualifiers':qualifiers}),
        'Edge':obj({'source':text,'relation':{'enum':sorted(RELATION_STATES)},'target':text,'qualifiers':qualifiers}),
        'DocumentClaim':obj({'id':text,'subject':text,'predicate':text,'object':scalar,
            'modality':{'enum':['asserted','possible','recommended','conditional','negated']},'conditions':{'type':'object','maxProperties':16,'additionalProperties':text}}),
    }
    branches=[]
    for name,payload in payloads.items():
        branches.append(obj({'id':text,'fact_kind':{'const':name},'payload':payload,
            'status':{'enum':['accepted','candidate','unresolved']},'support_ids':{'type':'array','items':text}}))
    return {'$schema':'https://json-schema.org/draft/2020-12/schema','title':'LightGraph snapshot 1.0',
        **obj({'format_version':{'const':'1.0'},'isolation_domain':text,'scopes':{'type':'array','items':text},
            'nodes':{'type':'array','items':{'type':'object'}},'edges':{'type':'array','items':{'type':'object'}},
            'claims':{'type':'array','items':{'type':'object'}},'facts':{'type':'array','items':{'oneOf':branches}},
            'supports':{'type':'array','items':{'type':'object'}},'publications':{'type':'array','items':text},'historical':{'type':'boolean'}})}
