from __future__ import annotations
import pytest
from lightgraph.contracts import (TableSpec,ColumnSpec,SourceRef,IdentitySpec,AttributeSpec,
    RuleSpec,ForeignKeySpec,Coverage,Dependencies,digest)
from lightgraph.metadata_alignment import discover_metadata_alignments
from lightgraph.store.policy import PersistencePolicy
from lightgraph.errors import LightGraphError


def table(name,cols=('code','title')):
    return TableSpec(source_id='s',name=name,columns=[ColumnSpec(name=c) for c in cols])


def rule(t,*,name=None,kind='Metric',label='title',status='validated'):
    return RuleSpec(rule_id=name or 'r_'+t.name,kind='column_group_entity',
        source=SourceRef(source_id=t.source_id,schema=t.schema_name,table=t.name),
        identity=IdentitySpec(key_columns=['code'],entity_kind=kind),
        attributes=AttributeSpec(single_valued={'label':label}),status=status,
        validation_ref='v_01' if status!='draft' else None,coverage=Coverage(consistent=True))


def test_aligns_different_field_names_only_when_rules_share_type_and_role():
    a=table('a',('code','cn_name'));b=table('b',('code','business_caption'));c=table('c',('code','cn_name'))
    result=discover_metadata_alignments([rule(a,label='cn_name'),rule(b,label='business_caption'),rule(c,kind='Dimension',label='cn_name')],[a,b,c])
    assert result.complete and len(result.candidates)==2
    label=next(x for x in result.candidates if x.role=='label')
    assert label.left.column=='cn_name' and label.right.column=='business_caption'
    assert label.status=='candidate'
    assert label.fact().payload['relation']=='same_attribute_role'
    assert {d.rule_id for d in label.dependencies}=={'r_a','r_b'}
    support=label.support('alignment:all')
    assert support.status=='candidate' and support.rule_id=='alignment:all'
    PersistencePolicy().check_fact(label.fact())
    assert all(c.fact().payload['relation']!='same_entity' for c in result.candidates)


def test_identical_names_without_validated_rules_produce_nothing():
    a,b=table('a'),table('b')
    result=discover_metadata_alignments([rule(a,status='draft'),rule(b,status='draft')],[a,b])
    assert not result.candidates and result.ignored_rules==2
    assert not discover_metadata_alignments([], [a,b]).candidates


def test_indexes_dedupe_physical_column_and_skip_within_table_pairs():
    a,b=table('a'),table('b')
    rules=[rule(a,name='a1'),rule(a,name='a2'),rule(b)]
    result=discover_metadata_alignments(rules,[a,b])
    assert len(result.candidates)==2
    assert all(len(c.dependencies)==3 for c in result.candidates)
    assert result.version==discover_metadata_alignments(list(reversed(rules)),[b,a]).version
    assert [c.alignment_id for c in result.candidates]==[c.alignment_id for c in discover_metadata_alignments(rules[::-1],[b,a]).candidates]


def test_bounded_generation_has_exact_deferred_counts_without_source_access():
    tables=[table(f't_{i:04}') for i in range(1000)]
    result=discover_metadata_alignments([rule(t) for t in tables],tables,max_candidates=17)
    assert len(result.candidates)==17
    assert result.candidate_total==1000*999 # label and identity_code buckets
    assert result.deferred_count==1000*999-17 and not result.complete
    assert result.counts()['alignment_deferred']==result.deferred_count


def test_newer_deprecated_revision_does_not_revive_prior_mapping():
    a,b=table('a'),table('b')
    old=rule(a);new=old.model_copy(deep=True);new.revision=2;new.status='deprecated'
    result=discover_metadata_alignments([old,new,rule(b)],[a,b])
    assert not result.candidates and result.ignored_rules==1


def test_stale_schema_mapping_is_reported_not_aligned():
    a,b=table('a'),table('b');r=rule(a)
    r.dependencies=Dependencies(schema='stale_fingerprint')
    result=discover_metadata_alignments([r,rule(b)],[a,b])
    assert not result.candidates and not result.complete
    assert result.issues[0]['code']=='alignment_rule_schema_stale'


def test_declared_composite_fk_is_accepted_separately_and_preserves_pairing():
    a=table('a',('tenant','ref'));b=table('b',('tenant','code'))
    a.foreign_keys=[ForeignKeySpec(name='fk_pair',columns=['tenant','ref'],target_schema='public',target_table='b',target_columns=['tenant','code'])]
    result=discover_metadata_alignments([],[a,b])
    assert not result.candidates
    assert [(f.left.column,f.right.column) for f in result.declared_foreign_keys]==[('tenant','tenant'),('ref','code')]
    assert len({f.declaration_id for f in result.declared_foreign_keys})==1
    assert all(f.status=='accepted' and f.fact().payload['relation']=='declared_fk' for f in result.declared_foreign_keys)
    for f in result.declared_foreign_keys:PersistencePolicy().check_fact(f.fact())
    limited=discover_metadata_alignments([],[a,b],max_declared_edges=1)
    assert limited.declared_deferred_count==1 and not limited.complete


def test_fk_to_outside_catalog_is_reported_without_dangling_fact():
    a=table('a');a.foreign_keys=[ForeignKeySpec(name='outside',columns=['code'],target_schema='private',target_table='secret',target_columns=['x'])]
    result=discover_metadata_alignments([],[a])
    assert not result.declared_foreign_keys
    assert result.issues[0]['code']=='declared_fk_target_unavailable'


def test_rejects_unbounded_candidate_limit():
    with pytest.raises(LightGraphError,match='alignment_limit_invalid'):
        discover_metadata_alignments([],[],max_candidates=1000000)
