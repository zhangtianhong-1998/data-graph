"""Read-only captions derived from authorized, accepted reference supports.

Captions are presentation, never identity or new property facts. A missing role,
ambiguous name, or withdrawn edge leaves the original code caption intact.
"""
from collections import defaultdict
import json

from lightgraph.contracts import canonical


def display_rules(store, graph):
    ids=sorted({s['rule_id'] for s in graph.get('supports',[]) if s['status']=='accepted'})
    result={}
    for offset in range(0,len(ids),400):
        chunk=ids[offset:offset+400]
        for rid,revision,payload in store.connection.execute(
                'SELECT rule_id,revision,payload FROM rule WHERE rule_id IN ('+
                ','.join('?' for _ in chunk)+')',chunk):
            result[rid,revision]=json.loads(payload)
    return result


def composite_columns(rule):
    identity=rule.get('identity',{});attrs=rule.get('attributes',{})
    keys=identity.get('key_columns',[]);namespaces=rule.get('scope',{}).get('namespaces',[])
    fields=[c for c in keys if c not in namespaces]
    if (rule.get('kind')!='column_group_entity' or
            identity.get('semantic_resolution')!='source_scoped' or len(fields)<2 or
            attrs.get('single_valued',{}).get('label') or attrs.get('name_columns',{}).get('label')):
        return []
    return fields


def project_display_labels(graph, rules):
    """Join exact published endpoints, keeping labels scoped to their supports."""
    nodes={n['id']:n for n in graph.get('nodes',[]) if n.get('status')=='accepted'}
    facts={f['id']:f for f in graph.get('facts',[]) if f.get('status')=='accepted'}
    supports=defaultdict(list);existence=defaultdict(list);properties=defaultdict(list);edges=defaultdict(list)
    for support in graph.get('supports',[]):
        if support.get('status')=='accepted' and support['fact_id'] in facts:
            supports[support['fact_id']].append(support)
    for fid,fact in facts.items():
        p=fact['payload']
        if fact['fact_kind']=='NodeExistence':existence[p['entity_id']].append(fid)
        elif fact['fact_kind']=='PropertyValue':properties[p['entity_id'],p['name']].append(fid)
        elif fact['fact_kind']=='Edge':edges[p['source']].append(fid)
    def contract(support):return rules.get((support['rule_id'],support['rule_revision']),{})
    def scoped_values(entity,name,scope,*,target_rule=None):
        found={};proof=set()
        for fid in properties[entity,name]:
            for s in supports[fid]:
                rule=contract(s);identity=rule.get('identity',{})
                global_name=(name=='label' and target_rule and
                    identity.get('semantic_resolution')=='verified_identity_domain' and
                    identity.get('identity_scope')=='domain_global' and identity.get('attribute_evidence') and
                    identity.get('identity_domain')==target_rule.get('identity',{}).get('identity_domain') and
                    identity.get('entity_kind')==target_rule.get('identity',{}).get('entity_kind'))
                if s['scope_ref']!=scope and not global_name:continue
                value=facts[fid]['payload']['value'];found[canonical(value)]=value;proof.add(fid)
        return list(found.values()),proof
    projected=0
    for nid,node in nodes.items():
        if node.get('layer')!='business':continue
        # Each source interpretation must produce the same complete caption.
        captions=set();proof=set();blocked=False;eligible=False
        for existence_id in existence[nid]:
            for support in supports[existence_id]:
                rule=contract(support);fields=composite_columns(rule)
                if not fields:continue
                eligible=True;scope=support['scope_ref'];keys=rule['identity']['key_columns']
                codes,code_refs=scoped_values(nid,'business_code',scope)
                try:values=json.loads(codes[0]) if len(codes)==1 and isinstance(codes[0],str) else None
                except (ValueError,TypeError):values=None
                if not isinstance(values,list) or len(values)!=len(keys):blocked=True;continue
                row=dict(zip(keys,values));roles=defaultdict(set);refs=set(code_refs)|{existence_id}
                for edge_id in edges[nid]:
                    edge=facts[edge_id]['payload'];target=edge['target']
                    if target not in nodes:continue
                    for es in supports[edge_id]:
                        if es['scope_ref']!=scope:continue
                        reference=contract(es);relation=reference.get('relation') or {}
                        if (reference.get('kind')!='reference' or
                                relation.get('source_rule_id')!=rule['rule_id'] or
                                reference.get('source')!=rule.get('source') or
                                relation.get('source_columns')!=keys or
                                relation.get('source_json_array_columns') or relation.get('target_json_array_columns')):continue
                        # A role is a specific component column, never a guessed
                        # meaning inferred from relation names or label strings.
                        local=relation.get('target_columns',[])
                        for target_existence in existence[target]:
                            for ts in supports[target_existence]:
                                trule=contract(ts)
                                if trule.get('rule_id')!=relation.get('target_rule_id'):continue
                                tkeys=trule.get('identity',{}).get('key_columns',[])
                                tnamespaces=trule.get('scope',{}).get('namespaces',[])
                                slots=[i for i,c in enumerate(tkeys) if c not in tnamespaces]
                                if len(slots)!=1 or len(local)!=len(tkeys) or local[slots[0]] not in fields:continue
                                column=local[slots[0]]
                                labels,label_refs=scoped_values(target,'label',scope,target_rule=trule)
                                if len(labels)!=1 or not isinstance(labels[0],str) or not labels[0]:
                                    blocked=True;continue
                                roles[column].add((target,labels[0]));refs.update(label_refs|{edge_id,target_existence})
                if any(len(roles[c])!=1 for c in fields):blocked=True;continue
                parts=[] if rule.get('scope',{}).get('namespaces') else [f'scope={scope}']
                for column in keys:
                    value=row[column]
                    if column in fields:
                        _,name=next(iter(roles[column]))
                        # Keep codes in every caption so same-name concepts
                        # remain distinguishable without merging identities.
                        parts.append(f'{column}={name} [{value}]')
                    else:parts.append(f'{column}={value}')
                captions.add(' · '.join(parts));proof.update(refs)
        if eligible and not blocked and len(captions)==1:
            node['display_label']=next(iter(captions))
            node['display_label_basis']={'method':'accepted_reference_components','fact_ids':sorted(proof)}
            projected+=1
    return projected
