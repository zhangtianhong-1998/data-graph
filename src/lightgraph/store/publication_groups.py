"""Publish complete rule generations independently only across real dependencies.

The grouping index uses already derived graph identities and supports, never source
rows. Old/new identities and surviving edges participate so deletion and migration
cannot sever a dependency that disappears from the incoming projection.
"""
from collections import defaultdict
from dataclasses import dataclass
import json

from ..errors import LightGraphError


@dataclass(frozen=True)
class PublicationGroup:
    publication_ids: tuple[str, ...]
    rule_ids: tuple[str, ...]


@dataclass(frozen=True)
class GroupConflict:
    code: str
    rule_ids: tuple[str, ...]
    fact_refs: tuple[str, ...] = ()


def _rows(store, ids):
    return store._fact_rows(list(ids)) if ids else []


def dependency_groups(store, publication_ids):
    """Group changed contributions, not every edge reachable in a business graph.

    Unchanged projections do not connect unrelated updates. Existing references
    matter when an endpoint is replaced/withdrawn; hierarchy paths are consulted
    only for topology changes. Full old/new generations remain indivisible.
    """
    from ..rules.topology import hierarchy_context
    pubs=[store._publication(p) for p in publication_ids]
    incoming={p['logical_rule_id'] for p in pubs}
    if not incoming:return []
    parents={}
    def find(key):
        parents.setdefault(key,key)
        while parents[key]!=key:
            parents[key]=parents[parents[key]];key=parents[key]
        return key
    def join(a,b):
        a,b=find(a),find(b)
        if a!=b:parents[b]=a
    for rid in incoming:find(('rule',rid))
    current=[h['publication_id'] for h in store.heads() if h['valid']]
    old=_rows(store,current);new=_rows(store,publication_ids)
    prospective=_rows(store,store._prospective_publications(pubs))
    old_by_rule=defaultdict(dict);new_by_rule=defaultdict(dict)
    def signature(r):return (r['fact_id'],r['binding_id'],r['binding_revision'],r['status'])
    for r in old:
        if r['rule_id'] in incoming:old_by_rule[r['rule_id']][signature(r)]=r
    for r in new:new_by_rule[r['rule_id']][signature(r)]=r
    changed=[]
    for rid in incoming:
        before,after=old_by_rule[rid],new_by_rule[rid]
        changed.extend((before.get(key) or after[key]) for key in before.keys() ^ after.keys())
    business=set();node_contracts=[]
    for rows in (old,prospective):
        contracts=defaultdict(set)
        for row in rows:
            p=json.loads(row['payload'])
            if row['fact_kind']=='NodeExistence' and p.get('layer')=='business':
                business.add((row['isolation_domain'],p['entity_id']))
                if row['status']!='deprecated':contracts[(row['isolation_domain'],p['entity_id'])].add((p['kind'],row['status']))
        node_contracts.append(contracts)
    contract_changes={e for e in node_contracts[0].keys()|node_contracts[1].keys()
                      if node_contracts[0][e]!=node_contracts[1][e]}
    active_topology=set();binding_cache={}
    for row in changed:
        rid=row['rule_id']
        if rid.startswith('document-alignment:'):continue
        p=json.loads(row['payload']);domain=row['isolation_domain']
        values=([p['entity_id']] if row['fact_kind'] in ('NodeExistence','PropertyValue')
                else [p['source'],p['target']] if row['fact_kind']=='Edge' else [])
        for entity in values:
            if (domain,entity) not in business:continue
            join(('rule',rid),('entity',domain,entity))
        binding=row['binding_id']
        if binding not in binding_cache:binding_cache[binding]=store.identity.lookup(binding) is not None
        if binding_cache[binding]:join(('rule',rid),('binding',binding))
        if row['fact_kind']=='Edge' and p['relation']=='parent_of':
            context=hierarchy_context(domain,p.get('qualifiers',{}));active_topology.add(context)
            for entity in values:join(('rule',rid),('topology',context,entity))
    # A stable edge does not connect unrelated attribute additions. It does
    # participate when its accepted endpoint contract is being replaced.
    for row in old+new:
        if row['fact_kind']!='Edge' or row['status']=='deprecated' or row['rule_id'].startswith('document-alignment:'):continue
        p=json.loads(row['payload']);domain=row['isolation_domain'];rid=row['rule_id']
        if rid in incoming:
            for entity in (p['source'],p['target']):
                if (domain,entity) in contract_changes:join(('rule',rid),('entity',domain,entity))
        if p['relation']=='parent_of':
            context=hierarchy_context(domain,p.get('qualifiers',{}))
            if context in active_topology:join(('topology',context,p['source']),('topology',context,p['target']))
    # The existing validator's unanimous DAG permission is taxonomy-wide.
    # Couple only contributors of an actual multi-parent/permission conflict.
    topology_rows=defaultdict(list)
    for row in prospective:
        if row['fact_kind']=='Edge':
            p=json.loads(row['payload'])
            if p['relation']=='parent_of':topology_rows[hierarchy_context(row['isolation_domain'],p.get('qualifiers',{}))].append(row)
    for rows in topology_rows.values():
        owners,_=_locate(store,rows,'multiple_parent',old)
        affected=sorted(set(owners)&incoming)
        for rid in affected[1:]:join(('rule',affected[0]),('rule',rid))
    # A changed permission can enable a multi-parent branch even when the
    # final prospective graph has no conflict. Its old strict support must be
    # replaced in the same transaction as the branch it enables.
    definitions={(r.rule_id,r.revision):r for r in store.get_rules(include_stale=True)}
    permission_states=[];multiparent_owners=defaultdict(set)
    for rows in (old,prospective):
        strict=defaultdict(set);branches=defaultdict(list)
        for row in rows:
            if row['fact_kind']!='Edge' or row['status']!='accepted':continue
            p=json.loads(row['payload'])
            if p['relation']!='parent_of':continue
            context=hierarchy_context(row['isolation_domain'],p.get('qualifiers',{}))
            definition=definitions.get((row['rule_id'],row['revision']))
            allow=(definition.hierarchy.allow_dag if definition and definition.hierarchy
                   else p.get('qualifiers',{}).get('allow_dag',False))
            if allow is not True:strict[context].add(row['rule_id'])
            branches[context].append((row['rule_id'],p['source'],p['target']))
        for context,edges in branches.items():
            sources=defaultdict(set)
            for rid,a,b in edges:sources[b].add(a)
            multiparent_owners[context].update(rid for rid,a,b in edges if len(sources[b])>1)
        permission_states.append(strict)
    for context,owners in multiparent_owners.items():
        if not owners:continue
        changed_permissions=(permission_states[0][context] ^ permission_states[1][context]) & incoming
        affected=sorted((owners & incoming)|changed_permissions)
        if changed_permissions:
            for rid in affected[1:]:join(('rule',affected[0]),('rule',rid))
    groups=defaultdict(list)
    for pub in pubs:groups[find(('rule',pub['logical_rule_id']))].append(pub)
    return sorted([PublicationGroup(tuple(p['publication_id'] for p in values),
        tuple(sorted({p['logical_rule_id'] for p in values}))) for values in groups.values()],key=lambda g:g.rule_ids)


def _cycle_edges(edges):
    """Kosaraju components without recursion, returning only edges in cycles."""
    forward=defaultdict(set);reverse=defaultdict(set)
    for a,b in edges:forward[a].add(b);reverse[b].add(a)
    visited=set();order=[]
    for node in set(forward)|set(reverse):
        if node in visited:continue
        stack=[(node,False)]
        while stack:
            n,done=stack.pop()
            if done:order.append(n);continue
            if n in visited:continue
            visited.add(n);stack.append((n,True))
            stack.extend((child,False) for child in forward[n] if child not in visited)
    components={};sizes=defaultdict(int)
    for node in reversed(order):
        if node in components:continue
        component=len(sizes);stack=[node]
        while stack:
            n=stack.pop()
            if n in components:continue
            components[n]=component;sizes[component]+=1
            stack.extend(reverse[n]-components.keys())
    return {(a,b) for a,b in edges if components[a]==components[b] and (a==b or sizes[components[a]]>1)}


def _locate(store, rows, code, old_rows):
    """Locate supports that make the validator's first conflict true.

    Return no guessed source values. Unknown validators fall back to the
    dependency group, never to every rule prepared in the run.
    """
    selected=[];decoded=[(r,json.loads(r['payload'])) for r in rows if r['status']!='deprecated']
    if code=='identity_domain_attribute_conflict':
        domains={r[0] for r in store.connection.execute("SELECT entity_id FROM entity_identity WHERE semantic_namespace LIKE 'identity_domain:%'")}
        grouped=defaultdict(list)
        for r,p in decoded:
            if r['fact_kind']=='PropertyValue' and r['status']=='accepted' and p['entity_id'] in domains and p['name'] not in {'alias','english_name','business_code','source_position','level','level_no'}:
                grouped[(r['isolation_domain'],p['entity_id'],p['name'])].append((r,p))
        for values in grouped.values():
            if len({json.dumps(p['value'],sort_keys=True,ensure_ascii=False) for r,p in values})>1:selected.extend(r for r,p in values)
    elif code in {'hierarchy_cycle','hierarchy_self_loop','multiple_parent'}:
        from ..rules.topology import hierarchy_context
        grouped=defaultdict(list);rules={(r.rule_id,r.revision):r for r in store.get_rules(include_stale=True)}
        for r,p in decoded:
            if r['fact_kind']=='Edge' and r['status']=='accepted' and p['relation']=='parent_of':
                grouped[hierarchy_context(r['isolation_domain'],p.get('qualifiers',{}))].append((r,p))
        for values in grouped.values():
            pairs={(p['source'],p['target']) for r,p in values}
            if code=='hierarchy_cycle':bad=_cycle_edges(pairs)
            elif code=='hierarchy_self_loop':bad={(a,b) for a,b in pairs if a==b}
            else:
                strict=[r for r,p in values if not (rules[(r['rule_id'],r['revision'])].hierarchy.allow_dag
                    if (r['rule_id'],r['revision']) in rules and rules[(r['rule_id'],r['revision'])].hierarchy
                    else p.get('qualifiers',{}).get('allow_dag',False))]
                parents=defaultdict(set)
                for a,b in pairs:parents[b].add(a)
                bad={(a,b) for a,b in pairs if strict and len(parents[b])>1}
                # The validator currently applies unanimous permission per taxonomy.
                if bad:selected.extend(strict)
            selected.extend(r for r,p in values if (p['source'],p['target']) in bad)
    elif code=='binding_revision_requires_dependency_publication':
        grouped=defaultdict(list)
        for r,p in decoded:
            binding=store.identity.lookup(r['binding_id'],r['binding_revision'])
            if binding:grouped[r['binding_id']].append((r,binding.entity_id))
        for values in grouped.values():
            if len({entity for r,entity in values})>1:selected.extend(r for r,e in values)
    elif code in {'dangling_edge_requires_dependency_publication','property_requires_visible_node_support','conflicting_entity_kinds','relation_endpoint_kind_mismatch','reference_self_link_forbidden','hierarchy_self_loop'}:
        nodes=defaultdict(list)
        for r,p in decoded:
            if r['fact_kind']=='NodeExistence':nodes[(r['isolation_domain'],p['entity_id'])].append((r,p))
        missing=set()
        if code=='conflicting_entity_kinds':
            for values in nodes.values():
                if len({p['kind'] for r,p in values})>1:selected.extend(r for r,p in values)
        for r,p in decoded:
            ids=[p['source'],p['target']] if r['fact_kind']=='Edge' else [p['entity_id']] if r['fact_kind']=='PropertyValue' else []
            absent=[(r['isolation_domain'],e) for e in ids if not any(n['status']=='accepted' or r['status']!='accepted' for n,_ in nodes[(r['isolation_domain'],e)])]
            if absent and code in {'dangling_edge_requires_dependency_publication','property_requires_visible_node_support'}:
                selected.append(r);missing.update(absent)
            elif r['fact_kind']=='Edge' and code=='relation_endpoint_kind_mismatch' and not absent:
                kinds=store.policy.relation_endpoint_kinds(p['relation'])
                if any(any(n['kind'] not in allowed for _,n in nodes[(r['isolation_domain'],entity)]) for entity,allowed in zip(ids,kinds)):
                    selected.append(r)
                    for entity in ids:selected.extend(n for n,_ in nodes[(r['isolation_domain'],entity)])
            elif r['fact_kind']=='Edge' and code=='reference_self_link_forbidden' and p['source']==p['target']:selected.append(r)
        for row in old_rows:
            p=json.loads(row['payload'])
            if row['fact_kind']=='NodeExistence' and (row['isolation_domain'],p['entity_id']) in missing:selected.append(row)
    return tuple(sorted({r['rule_id'] for r in selected})),tuple(sorted({r['fact_id'] for r in selected}))


def inspect_group(store, group, *, ignore_derived_alignments=False):
    """Check against surviving heads and report the actual conflicting supports."""
    pubs=[store._publication(p) for p in group.publication_ids]
    ids=store._prospective_publications(pubs)
    if ignore_derived_alignments:
        ids=[p for p in ids if not store._publication(p)['logical_rule_id'].startswith('document-alignment:')]
    try:store._validate_endpoints(ids)
    except LightGraphError as exc:
        old=[h['publication_id'] for h in store.heads() if h['valid']]
        rules,facts=_locate(store,_rows(store,ids),exc.code,_rows(store,old))
        return GroupConflict(exc.code,rules or group.rule_ids,facts[:64])
    return None


def retire_dangling_document_alignments(store):
    """Retire only derived document heads whose endpoints actually disappeared."""
    heads=[h for h in store.heads() if h['valid']]
    rows=_rows(store,[h['publication_id'] for h in heads])
    existing={(r['isolation_domain'],json.loads(r['payload'])['entity_id']) for r in rows
              if r['fact_kind']=='NodeExistence' and r['status']=='accepted'}
    affected=set()
    for row in rows:
        if row['fact_kind']!='Edge' or not row['rule_id'].startswith('document-alignment:'):continue
        payload=json.loads(row['payload'])
        if any((row['isolation_domain'],payload[e]) not in existing for e in ('source','target')):
            affected.add(row['rule_id'])
    for rid in affected:store.invalidate_rule(rid,'business_requires_review')
    return affected
