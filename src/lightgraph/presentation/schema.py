"""Machine-readable snapshot schema, derived from the persistence registry."""
from ..store.policy import PROPERTY_NAMES,QUALIFIER_NAMES,KINDS,RELATION_STATES
from ..store.policy import PersistencePolicy
from ..semantic_schema import SemanticSchema


def snapshot_schema(semantic_schema=None):
    definitions=SemanticSchema.model_validate(semantic_schema or {})
    policy=PersistencePolicy().with_schema(definitions)
    scalar={'type':['string','number','boolean','null']}
    qualifiers={'type':'object','propertyNames':{'enum':sorted(QUALIFIER_NAMES)},'additionalProperties':scalar}
    def obj(fields,required=None):return {'type':'object','properties':fields,'required':required or list(fields),'additionalProperties':False}
    text={'type':'string'}
    payloads={
        'NodeExistence':obj({'entity_id':text,'kind':{'enum':sorted(set().union(*policy.kinds.values()))},'layer':{'enum':list(KINDS)}}),
        'PropertyValue':obj({'entity_id':text,'name':{'enum':sorted(PROPERTY_NAMES|set(policy.property_definitions))},'value':scalar,'qualifiers':qualifiers}),
        'Edge':obj({'source':text,'relation':{'enum':sorted(set(RELATION_STATES)|set(policy.relation_definitions))},'target':text,'qualifiers':qualifiers}),
        'DocumentClaim':obj({'id':text,'subject':text,'predicate':text,'object':scalar,
            'modality':{'enum':['asserted','possible','recommended','conditional','negated']},'conditions':{'type':'object','maxProperties':16,'additionalProperties':text}}),
    }
    if policy.property_definitions:
        payloads['PropertyValue']['allOf']=[{
            'if':{'properties':{'name':{'const':name}}},
            'then':{'properties':{'value':{'type':[definition.value_type,'null']}}}
        } for name,definition in sorted(policy.property_definitions.items())]
    branches=[]
    for name,payload in payloads.items():
        branches.append(obj({'id':text,'fact_kind':{'const':name},'payload':payload,
            'status':{'enum':['accepted','candidate','unresolved']},'support_ids':{'type':'array','items':text}}))
    result={'$schema':'https://json-schema.org/draft/2020-12/schema','title':'LightGraph snapshot 1.0',
        **obj({'format_version':{'const':'1.0'},'isolation_domain':text,'scopes':{'type':'array','items':text},
            'nodes':{'type':'array','items':{'type':'object'}},'edges':{'type':'array','items':{'type':'object'}},
            'claims':{'type':'array','items':{'type':'object'}},'facts':{'type':'array','items':{'oneOf':branches}},
            'supports':{'type':'array','items':{'type':'object'}},'publications':{'type':'array','items':text},'historical':{'type':'boolean'}})}
    vocabulary=SemanticSchema.model_json_schema()
    result['$defs']=vocabulary.pop('$defs',{})
    result['properties']['semantic_schema']=vocabulary
    if not definitions.is_empty():result['required'].append('semantic_schema')
    return result
