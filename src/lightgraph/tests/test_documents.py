import pytest
from lightgraph.documents import load_document,extract_claims,DocumentCorpus,changed_sections,ClaimBatch,ClaimProposal
from lightgraph.contracts import SourceRef
from lightgraph.errors import LightGraphError


def test_metadata_version_sections_and_fence_headings():
    text='---\nid: guide\nrevision: r1\npurpose: data_description\napplies_to: [public.items]\n---\n# 字段\n编码说明\n```sql\n# not heading\nselect 1\n```\n## 条件\n仅在同一租户中。'
    d=load_document('guide.md',text=text)
    assert d.document_id=='guide' and d.revision.startswith('r1:')
    assert len(d.sections)==2
    assert d.sections[0].line_start==7
    assert d.scope.tables==['public.items']
    revised=load_document('guide.md',text=text.replace('编码说明','新的编码说明'))
    assert d.revision!=revised.revision
    delta=changed_sections(d,revised)
    assert len(delta['unchanged'])==1 and len(delta['added'])==1


def test_description_context_scoped_and_derived_not_reused_as_evidence():
    text='---\npurpose: data_description\napplies_to: [public.items]\n---\n# 定义\nitems.code 是编码。'
    d=load_document('doc.md',text=text)
    c=DocumentCorpus([d])
    assert c.context_for(SourceRef(source_id='s',table='items'),['code'])
    assert not c.context_for(SourceRef(source_id='s',table='another'),['code'])
    d.derived=True
    assert not c.context_for(SourceRef(source_id='s',table='items'),['code'])


def test_analytical_modal_claim_and_conditions_preserved():
    d=load_document('analysis.md',text='---\npurpose: analysis_knowledge\n---\n# 费用\n当销售费用上升时，建议按区域检查费用构成。\n# 收入\n收入下降可能与产品结构变化有关。')
    claims=extract_claims(d)
    assert len(claims)==2
    assert claims[0].modality=='recommended'
    assert claims[0].conditions==['当销售费用上升时']
    assert claims[1].modality=='possible' and claims[1].predicate=='related_to'
    assert all(c.status=='candidate' and c.alignment.status=='unresolved' for c in claims)


def test_model_cannot_upgrade_possible_to_asserted():
    d=load_document('analysis.md',text='---\npurpose: analysis_knowledge\n---\n# 因素\n价格可能影响收入。')
    class Model:
        def complete(self,*args):
            return ClaimBatch(claims=[ClaimProposal(section_id=d.sections[0].section_id,subject='价格',predicate='may_affect',object='收入',modality='asserted',evidence_text='价格可能影响收入')])
    with pytest.raises(LightGraphError,match='document_claim_modality_upgrade'):extract_claims(d,Model())


def test_model_cannot_invent_document_evidence():
    d=load_document('analysis.md',text='---\npurpose: analysis_knowledge\n---\n# 因素\n价格可能影响收入。')
    class Model:
        def complete(self,*args):
            return ClaimBatch(claims=[ClaimProposal(section_id=d.sections[0].section_id,subject='价格',predicate='may_affect',object='利润',modality='possible',evidence_text='价格可能影响利润')])
    with pytest.raises(LightGraphError,match='ungrounded_quote'):extract_claims(d,Model())


def test_unknown_frontmatter_and_yaml_expansion_rejected():
    with pytest.raises(LightGraphError,match='unknown_document_front_matter_field'):
        load_document('doc.md',text='---\nexecute_sql: delete all\n---\n# text')
    with pytest.raises(LightGraphError,match='document_yaml_alias_or_tag_forbidden'):
        load_document('doc.md',text='---\ntitle: &a [x]\nscope: *a\n---\n# text')


def test_active_html_never_executed_or_fetched():
    d=load_document('doc.md',text='# 普通文档\n<script>fetch("https://bad.example")</script>')
    assert 'active_content_treated_as_text' in d.warnings
    assert d.purpose=='unresolved'
    with pytest.raises(LightGraphError,match='document_extract_incomplete'):extract_claims(d)


def test_no_claim_extraction_from_data_description():
    d=load_document('doc.md',text='---\npurpose: data_description\n---\n# 描述\n价格可能影响收入。')
    assert extract_claims(d)==[]


def test_abstain_and_oversize_are_incomplete_not_empty_success():
    d=load_document('a.md',text='---\npurpose: analysis_knowledge\n---\n# Topic\n建议检查收入。')
    class Abstain:
        def complete(self,*args):return ClaimBatch(abstain=True)
    with pytest.raises(LightGraphError,match='document_extract_incomplete'):extract_claims(d,Abstain())
    big=load_document('b.md',text='---\npurpose: analysis_knowledge\n---\n# Topic\n'+'分析'*7000)
    with pytest.raises(LightGraphError,match='document_extract_incomplete'):extract_claims(big)


def test_unhandled_analytical_sentence_is_not_complete_extraction():
    d=load_document('a.md',text='---\npurpose: analysis_knowledge\n---\n# 方法\n价格可能影响收入。\n此处另有尚未支持的复杂公式推导内容。')
    with pytest.raises(LightGraphError,match='document_extract_incomplete'):extract_claims(d)


def test_parent_conditions_are_kept_with_child_claim():
    d=load_document('a.md',text='---\npurpose: analysis_knowledge\n---\n# 方法\n适用条件：同一统计期间。\n## 影响\n价格可能影响收入。')
    claims=extract_claims(d)
    assert '适用条件：同一统计期间。' in claims[0].conditions
