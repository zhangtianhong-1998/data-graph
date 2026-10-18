"""Bounded column-role alignments derived from validated rules and catalog data.

No source query or value overlap is performed here. Equal roles generate reviewable
column relationships; they never establish that two business entities are equal.
"""
from __future__ import annotations
from collections import defaultdict
from itertools import combinations, product
from typing import Literal
from pydantic import Field
from .contracts import StrictModel, GraphFact, SupportSpec, RuleSpec, TableSpec, digest
from .errors import LightGraphError
from .metadata import metadata_id


class ColumnEndpoint(StrictModel):
    source_id: str
    schema_name: str
    table: str
    column: str
    @property
    def table_key(self): return (self.source_id,self.schema_name,self.table)
    @property
    def key(self): return (*self.table_key,self.column)
    def node_id(self,domain):
        return metadata_id(domain,self.source_id,self.schema_name+'.'+self.table,self.column)


class RuleDependency(StrictModel):
    rule_id: str
    revision: int
    validation_ref: str


class AlignmentCandidate(StrictModel):
    alignment_id: str
    left: ColumnEndpoint
    right: ColumnEndpoint
    entity_kind: str
    role: str
    relation: Literal['same_attribute_role']='same_attribute_role'
    status: Literal['candidate']='candidate'
    dependencies: list[RuleDependency]

    def fact(self,domain='default') -> GraphFact:
        return GraphFact(isolation_domain=domain,fact_kind='Edge',payload={
            'source':self.left.node_id(domain),'relation':self.relation,'target':self.right.node_id(domain),
            'qualifiers':{'role':self.role,'semantic_basis':'validated_role_assignment:'+self.entity_kind},
        })

    def support(self,publication_rule_id: str, *, revision: int=1, scope_ref: str='public') -> SupportSpec:
        """Bind to the alignment publication, whose versions include BOTH endpoints.

        Do not attach two independent supports, one per endpoint rule: one would
        incorrectly survive withdrawal of the other rule.
        """
        return SupportSpec(origin='catalog',source_binding=self.alignment_id,scope_ref=scope_ref,
            partition_id='all',rule_id=publication_rule_id,rule_revision=revision,status='candidate')


class DeclaredForeignKey(StrictModel):
    declaration_id: str
    left: ColumnEndpoint
    right: ColumnEndpoint
    constraint_name: str
    key_position: int
    key_arity: int
    status: Literal['accepted']='accepted'
    def fact(self,domain='default') -> GraphFact:
        return GraphFact(isolation_domain=domain,fact_kind='Edge',payload={
            'source':self.left.node_id(domain),'relation':'declared_fk','target':self.right.node_id(domain),
            'qualifiers':{'relation_group':self.declaration_id},
        })
    def support(self,publication_rule_id: str, *, revision: int=1, scope_ref: str='public') -> SupportSpec:
        return SupportSpec(origin='catalog',source_binding=self.declaration_id,scope_ref=scope_ref,
            partition_id='all',rule_id=publication_rule_id,rule_revision=revision,status='accepted')


class AlignmentResult(StrictModel):
    version: str
    candidates: list[AlignmentCandidate]=Field(default_factory=list)
    declared_foreign_keys: list[DeclaredForeignKey]=Field(default_factory=list)
    candidate_total: int=0
    deferred_count: int=0
    declared_total: int=0
    declared_deferred_count: int=0
    indexed_columns: int=0
    ignored_rules: int=0
    issues: list[dict[str,str|int]]=Field(default_factory=list)
    @property
    def complete(self):
        return self.deferred_count==0 and self.declared_deferred_count==0 and not self.issues
    def counts(self):
        return {'alignment_candidates':len(self.candidates),'alignment_candidate_total':self.candidate_total,
                'alignment_deferred':self.deferred_count,'declared_fk_columns':len(self.declared_foreign_keys),
                'declared_fk_deferred':self.declared_deferred_count,'alignment_indexed_columns':self.indexed_columns,
                'alignment_ignored_rules':self.ignored_rules}


def _roles(rule: RuleSpec):
    """Explicit semantic assignments only; field names do not create roles."""
    result={}
    excluded={e.column for e in rule.attributes.excluded}
    for role,column in {**rule.attributes.single_valued,**rule.attributes.multi_valued}.items():
        if column not in excluded: result[(role,column)]=None
    keys=[c for c in rule.identity.key_columns if c not in rule.scope.namespaces and c not in excluded]
    if len(keys)==1:result[('identity_code',keys[0])]=None
    if rule.hierarchy:
        for level in rule.hierarchy.levels:
            # Positions are qualified: L2 and L3 are not automatically aligned.
            if level.code not in excluded:result[(f'level_{level.level}.identity_code',level.code)]=None
            if level.label and level.label not in excluded:result[(f'level_{level.level}.label',level.label)]=None
            for alias in level.aliases:
                if alias not in excluded:result[(f'level_{level.level}.alias',alias)]=None
    return result


def discover_metadata_alignments(rules: list[RuleSpec], tables: list[TableSpec], *, domain: str='default', max_candidates: int=500, max_declared_edges: int=5000) -> AlignmentResult:
    """Build O(columns) role indexes and emit at most the configured pair cap.

    Deferred counts are exact combinatorial counts; un-emitted pairs are never
    materialized. Only cross-table pairs within the same entity-kind/role bucket
    are considered. Caller must reconcile one complete publication using result
    version; a partial result must not imply absence of older alignments.
    """
    if type(max_candidates) is not int or not 0<=max_candidates<=100000 or type(max_declared_edges) is not int or not 0<=max_declared_edges<=100000:
        raise LightGraphError('alignment_limit_invalid')
    catalog={(t.source_id,t.schema_name,t.name):t for t in tables}
    if len(catalog)!=len(tables):raise LightGraphError('alignment_catalog_duplicate')
    # Select latest revision before status filtering: a deprecated newer rule
    # must not accidentally revive an earlier validated mapping.
    latest={}
    for rule in rules:
        if rule.rule_id not in latest or rule.revision>latest[rule.rule_id].revision:latest[rule.rule_id]=rule
    version=digest('metadata_alignment_v1',domain,max_candidates,max_declared_edges,
        [t.model_dump(mode='json',by_alias=True) for _,t in sorted(catalog.items())],
        [r.model_dump(mode='json',by_alias=True) for _,r in sorted(latest.items())])
    out=AlignmentResult(version=version)
    buckets=defaultdict(dict)
    for rule in latest.values():
        if rule.status not in {'validated','canary_checked','published'} or not rule.validation_ref or rule.kind=='reference':
            out.ignored_rules+=1;continue
        key=(rule.source.source_id,rule.source.schema_name,rule.source.table)
        table=catalog.get(key)
        if table is None:
            out.issues.append({'code':'alignment_rule_source_unavailable','rule_id':rule.rule_id});continue
        if rule.dependencies.schema_ref and rule.dependencies.schema_ref != digest(table.model_dump(mode='json',by_alias=True)):
            out.issues.append({'code':'alignment_rule_schema_stale','rule_id':rule.rule_id});continue
        dependency=RuleDependency(rule_id=rule.rule_id,revision=rule.revision,validation_ref=rule.validation_ref)
        for (role,column) in _roles(rule):
            if not role or len(role)>128 or column not in table.column_names:
                out.issues.append({'code':'alignment_role_invalid','rule_id':rule.rule_id});continue
            endpoint=ColumnEndpoint(source_id=key[0],schema_name=key[1],table=key[2],column=column)
            entries=buckets[(rule.identity.entity_kind,role)]
            if endpoint.key not in entries:entries[endpoint.key]=(endpoint,{})
            entries[endpoint.key][1][(rule.rule_id,rule.revision)]=dependency
    for (kind,role),entries in sorted(buckets.items()):
        out.indexed_columns+=len(entries)
        grouped=defaultdict(list)
        for _,(endpoint,dependencies) in sorted(entries.items()):grouped[endpoint.table_key].append((endpoint,dependencies))
        n=len(entries)
        possible=(n*(n-1)-sum(len(v)*(len(v)-1) for v in grouped.values()))//2
        out.candidate_total+=possible
        remaining=max_candidates-len(out.candidates)
        if remaining<=0:continue
        emitted=0
        for left_table,right_table in combinations(sorted(grouped),2):
            if emitted>=remaining:break
            for (left,ldeps),(right,rdeps) in product(grouped[left_table],grouped[right_table]):
                if emitted>=remaining:break
                dependencies={**ldeps,**rdeps}
                out.candidates.append(AlignmentCandidate(alignment_id='alignment:'+digest(domain,kind,role,left.key,right.key)[:24],
                    left=left,right=right,entity_kind=kind,role=role,
                    dependencies=[d for _,d in sorted(dependencies.items())]))
                emitted+=1
    out.deferred_count=out.candidate_total-len(out.candidates)
    for key,table in sorted(catalog.items()):
        for fk in table.foreign_keys:
            target_key=(table.source_id,fk.target_schema,fk.target_table)
            target=catalog.get(target_key)
            if target is None or len(fk.columns)!=len(fk.target_columns) or not set(fk.columns)<=set(table.column_names) or not set(fk.target_columns)<=set(target.column_names):
                out.issues.append({'code':'declared_fk_target_unavailable','table':table.qualified_name});continue
            arity=len(fk.columns)
            out.declared_total+=arity
            declaration='declared_fk:'+digest(domain,key,fk.name,fk.columns,target_key,fk.target_columns)[:24]
            for position,(left,right) in enumerate(zip(fk.columns,fk.target_columns)):
                if len(out.declared_foreign_keys)>=max_declared_edges:break
                out.declared_foreign_keys.append(DeclaredForeignKey(declaration_id=declaration,
                    left=ColumnEndpoint(source_id=key[0],schema_name=key[1],table=key[2],column=left),
                    right=ColumnEndpoint(source_id=target_key[0],schema_name=target_key[1],table=target_key[2],column=right),
                    constraint_name=fk.name,key_position=position,key_arity=arity))
    out.declared_deferred_count=out.declared_total-len(out.declared_foreign_keys)
    return out
