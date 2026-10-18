"""P0 section-cache acceptance: no full-document model replay for a sibling edit."""
from types import SimpleNamespace
import pytest
from pydantic import ValidationError
from lightgraph.documents import (load_document,extract_claims,extract_with_cache,ClaimCache,
    ClaimBatch,ClaimProposal)
from lightgraph.errors import LightGraphError

BASE='''---
id: analysis-guide
revision: r1
purpose: analysis_knowledge
scope:
  tables: [public.items]
---
# 价格分析
价格可能影响收入。
# 成本分析
成本可能影响利润。
'''

class Model:
    config=SimpleNamespace(model='offline-test-model')
    def __init__(self):self.calls=[];self.abstain_on=None
    def complete(self,task,packet,response_model):
        section=packet['sections'][0]
        self.calls.append(section['section_id'])
        if self.abstain_on and self.abstain_on in section['text']:return ClaimBatch(abstain=True)
        text=section['text']
        for subject,obj in [('价格','收入'),('成本','利润'),('费用','利润')]:
            quote=f'{subject}可能影响{obj}'
            if quote in text:
                return ClaimBatch(claims=[ClaimProposal(section_id=section['section_id'],subject=subject,
                    predicate='may_affect',object=obj,modality='possible',evidence_text=quote)])
        return ClaimBatch()


def test_edit_one_section_calls_model_once_and_rebinds_current_provenance():
    first=load_document('guide.md',text=BASE)
    model=Model();before=extract_with_cache(first,model,known_entities={'价格':['old_entity']})
    assert len(model.calls)==2 and before.extracted_sections==2
    changed=load_document('guide.md',text=BASE.replace('成本可能影响利润','费用可能影响利润').replace('revision: r1','revision: r2'))
    after=extract_with_cache(changed,model,known_entities={'价格':['current_entity']},cached_sections=before.cache)
    assert len(model.calls)==3
    assert after.reused_sections==1 and after.extracted_sections==1
    unchanged=next(c for c in after.claims if c.subject=='价格')
    original=next(c for c in before.claims if c.subject=='价格')
    assert unchanged.claim_id==original.claim_id
    assert unchanged.document.revision==changed.revision!=first.revision
    assert unchanged.alignment.entity_id=='current_entity'
    assert unchanged.evidence_text==''
    assert {c.subject for c in after.claims}=={'价格','费用'}


def test_unchanged_document_zero_model_calls_and_no_evidence_text_in_cache():
    doc=load_document('guide.md',text=BASE+'\n')
    model=Model();first=extract_with_cache(doc,model)
    serialized=first.cache.model_dump_json()
    assert 'evidence_text' not in serialized
    assert '可能影响' not in serialized
    assert 'prompt' not in serialized and 'sections' not in serialized
    loaded=ClaimCache.model_validate_json(serialized)
    next_claims=extract_claims(doc,model,cached_sections=loaded)
    assert len(model.calls)==2
    assert {c.claim_id for c in next_claims}=={c.claim_id for c in first.claims}
    assert all(c.evidence_text=='' for c in next_claims)


def test_parent_condition_change_invalidates_descendant_cache():
    text=BASE.split('# 价格分析')[0]+'# 总体条件\n适用条件：同一统计期间。\n## 价格分析\n价格可能影响收入。\n## 成本分析\n成本可能影响利润。\n'
    first=load_document('guide.md',text=text)
    model=Model();before=extract_with_cache(first,model)
    assert len(model.calls)==3
    revised=load_document('guide.md',text=text.replace('同一统计期间','同一会计年度'))
    after=extract_with_cache(revised,model,cached_sections=before.cache)
    assert len(model.calls)==6
    assert after.reused_sections==0 and after.extracted_sections==3
    assert all('适用条件：同一会计年度。' in c.conditions for c in after.claims)
    assert all('同一统计期间' not in condition for c in after.claims for condition in c.conditions)


def test_scope_or_purpose_change_invalidates_unchanged_text():
    model=Model();first=extract_with_cache(load_document('guide.md',text=BASE),model)
    scoped=load_document('guide.md',text=BASE.replace('public.items','public.other_items'))
    after=extract_with_cache(scoped,model,cached_sections=first.cache)
    assert after.reused_sections==0 and len(model.calls)==4
    mixed=load_document('guide.md',text=BASE.replace('analysis_knowledge','mixed'))
    another=extract_with_cache(mixed,model,cached_sections=first.cache)
    assert another.reused_sections==0 and len(model.calls)==6
    description=load_document('guide.md',text=BASE.replace('analysis_knowledge','data_description'))
    empty=extract_with_cache(description,model,cached_sections=first.cache)
    assert empty.complete and empty.claims==[] and empty.cache.entries=={}
    assert len(model.calls)==6


def test_model_semantic_version_change_invalidates_cache():
    first_model=Model();doc=load_document('guide.md',text=BASE)
    first=extract_with_cache(doc,first_model)
    second_model=Model();second_model.config=SimpleNamespace(model='new-model')
    after=extract_with_cache(doc,second_model,cached_sections=first.cache)
    assert after.reused_sections==0 and len(second_model.calls)==2


def test_deleted_sections_removed_and_line_shift_updates_locator():
    model=Model();first_doc=load_document('guide.md',text=BASE)
    first=extract_with_cache(first_doc,model)
    revised=load_document('guide.md',text=BASE.replace('# 价格分析\n价格可能影响收入。\n',''))
    after=extract_with_cache(revised,model,cached_sections=first.cache)
    assert after.reused_sections==1 and after.extracted_sections==0
    assert len(after.cache.entries)==1
    assert len(after.claims)==1 and after.claims[0].subject=='成本'
    old=next(c for c in first.claims if c.subject=='成本')
    assert old.document.locator!=after.claims[0].document.locator


def test_incomplete_changed_section_never_returns_replacement_cache():
    model=Model();doc=load_document('guide.md',text=BASE)
    first=extract_with_cache(doc,model)
    before=first.cache.model_dump_json()
    changed=load_document('guide.md',text=BASE.replace('成本可能影响利润','费用可能影响利润'))
    model.abstain_on='费用'
    with pytest.raises(LightGraphError,match='document_extract_incomplete'):
        extract_with_cache(changed,model,cached_sections=first.cache)
    assert first.cache.model_dump_json()==before


def test_model_empty_for_unknown_analytical_section_does_not_cache_completeness():
    model=Model();doc=load_document('guide.md',text=BASE)
    first=extract_with_cache(doc,model)
    changed=load_document('guide.md',text=BASE.replace('成本可能影响利润','尚未解释的复杂分析推导'))
    with pytest.raises(LightGraphError,match='document_extract_incomplete'):
        extract_with_cache(changed,model,cached_sections=first.cache)


def test_raw_or_non_candidate_cache_payload_rejected():
    model=Model();doc=load_document('guide.md',text=BASE)
    result=extract_with_cache(doc,model)
    entry=next(iter(result.cache.entries))
    payload=result.cache.model_dump()
    payload['entries'][entry]['claims'][0]['evidence_text']='RAW_SOURCE_BODY'
    with pytest.raises(ValidationError):ClaimCache.model_validate(payload)
    payload=result.cache.model_dump()
    payload['entries'][entry]['claims'][0]['status']='accepted'
    with pytest.raises(ValidationError):ClaimCache.model_validate(payload)
    with pytest.raises(LightGraphError,match='typed_claim_cache_required'):
        extract_with_cache(doc,model,cached_sections={'raw_records':[['SECRET']]})


def test_ancestor_dependent_semantic_support_cannot_be_reused_after_parent_removed():
    text=BASE.split('# 价格分析')[0]+'# 限定\n适用条件：仅限国内业务。\n## 价格分析\n价格可能影响收入。\n'
    model=Model();first=extract_with_cache(load_document('guide.md',text=text),model)
    revised=load_document('guide.md',text=BASE.split('# 价格分析')[0]+'# 价格分析\n价格可能影响收入。\n')
    after=extract_with_cache(revised,model,cached_sections=first.cache)
    assert after.reused_sections==0 and after.extracted_sections==1
    assert after.claims[0].conditions==[]
