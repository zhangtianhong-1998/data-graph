"""Score independent coded concepts and exact joint assignments, without inference.

Role-to-kind and role-to-relation mappings are explicit evaluator inputs. The
scorer never searches mappings for a better score, or expands pairwise edges.
"""
from collections import Counter, defaultdict
from .core import load, fingerprint, scoped_nodes, scalars, score
from lightgraph.errors import LightGraphError


def evaluate_concepts(graph_path,gold_path,*,role_kinds,role_relations,context_kind):
    graph=load(graph_path);gold=load(gold_path)
    roles=sorted({c['role'] for c in gold.get('concepts',[])})
    if (gold.get('version')!='lightgraph.concepts-gold/1' or not roles or
        set(role_kinds)!=set(roles) or set(role_relations)!=set(roles) or
        len(set(role_kinds.values()))!=len(roles) or context_kind in role_kinds.values() or
        len(set(role_relations.values()))!=len(roles)):
        raise LightGraphError('concept_evaluation_mapping_invalid')
    expected={(c['role'],c['code']):c['label'] for c in gold['concepts']}
    expected_contexts={tuple([c['subject']]+[c[r] for r in roles]) for c in gold['contexts']}
    if len(expected)!=len(gold['concepts']) or len(expected_contexts)!=len(gold['contexts']):
        raise LightGraphError('concept_gold_duplicate')
    if any((r,c[r]) not in expected for c in gold['contexts'] for r in roles):
        raise LightGraphError('concept_gold_endpoint_invalid')
    kind_roles={kind:role for role,kind in role_kinds.items()}
    def evaluate_mode(statuses):
        nodes=scoped_nodes(graph,statuses,set(role_kinds.values())|{context_kind})
        by_key=defaultdict(list);keys={};instances=0
        ids=Counter(n['id'] for n in graph.get('nodes',[]) if n.get('status') in statuses and n.get('layer')=='business')
        for nid,node in nodes.items():
            if node.get('kind') not in kind_roles:continue
            instances+=ids[nid]
            codes=list(scalars(node.get('properties',{}).get('business_code',[])))
            if len(set(codes))!=1 or not all(isinstance(c,str) and c for c in codes) or ids[nid]!=1:continue
            key=(kind_roles[node['kind']],codes[0]);by_key[key].append(nid)
        for key,nids in by_key.items():
            if key in expected and len(nids)==1:keys[nids[0]]=key
        label_matches=sum(set(scalars(nodes[nid].get('label',[])))=={expected[key]} for nid,key in keys.items())
        facts={f['id']:f for f in graph.get('facts',[]) if f.get('status') in statuses and f.get('fact_kind')=='NodeExistence'}
        scopes=defaultdict(set)
        for support in graph.get('supports',[]):
            fact=facts.get(support.get('fact_id'))
            if fact and support.get('status') in statuses:scopes[fact['payload']['entity_id']].add(support['scope_ref'])
        edges=defaultdict(list)
        for edge in graph.get('edges',[]):
            if edge.get('status') in statuses:edges[edge['source']].append(edge)
        contexts=Counter();invalid=0
        for nid,node in nodes.items():
            if node.get('kind')!=context_kind:continue
            bindings={r:[] for r in roles};bad=ids[nid]!=1
            for edge in edges[nid]:
                role=next((r for r,rel in role_relations.items() if rel==edge.get('relation')),None)
                if role is None:continue
                key=keys.get(edge['target'])
                if not key or key[0]!=role:bad=True
                else:bindings[role].append(key[1])
            if any(len(v)!=1 for v in bindings.values()) or not scopes[nid]:bad=True
            if bad:invalid+=ids[nid];continue
            # Each authorized support scope is an explicit subject assignment;
            # unrecognized scopes count as false positives rather than vanish.
            for subject in scopes[nid]:contexts[tuple([subject]+[bindings[r][0] for r in roles])]+=1
        matched=set(contexts)&expected_contexts
        return {'concepts':score(len(keys),instances,len(expected)).model_dump(),
            'labels':score(label_matches,len(keys),len(expected)).model_dump(),
            'joint_contexts':score(len(matched),sum(contexts.values())+invalid,len(expected_contexts)).model_dump(),
            'invalid_context_nodes':invalid,'duplicate_context_instances':sum(v-1 for v in contexts.values()),
            'duplicate_concept_groups':[{'role':r,'code':c,'node_ids':n} for (r,c),n in by_key.items() if len(n)>1]}
    return {'version':'lightgraph.concepts-evaluation/1','graph_sha256':fingerprint(graph_path),
        'gold_sha256':fingerprint(gold_path),'role_kinds':role_kinds,'role_relations':role_relations,
        'context_kind':context_kind,'accepted':evaluate_mode({'accepted'}),
        'accepted_plus_candidates':evaluate_mode({'accepted','candidate','unresolved'}),
        'acceptance_boundary':'Exact coded concepts and observed subject-role tuples; mappings are explicit, not inferred. No pairwise Cartesian expansion.'}
