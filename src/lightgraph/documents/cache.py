"""Section-level cache of final candidate claim projections, never source text.

Cache persistence is the orchestrator's responsibility and should follow successful
publication. An incomplete extraction raises before returning any replacement
cache, so an earlier complete document publication remains recoverable.
"""
from __future__ import annotations
from typing import Literal, Annotated
from pydantic import Field, model_validator
from lightgraph.contracts import StrictModel,digest
from lightgraph.errors import LightGraphError
from lightgraph.context import CONTEXT_SELECTION_VERSION, STAGE_SUBTASKS, goal_payload
from .core import (KnowledgeDocument, DocumentSection, DocumentClaim, DocumentReference,
                   ClaimAlignment, DocumentScope, Purpose, _extract_uncached)

OPERATOR_VERSION='document_claim_extraction/3'
BoundedCondition=Annotated[str,Field(max_length=1024)]

class CachedClaim(StrictModel):
    """Only properties already accepted as a candidate analytical claim may persist."""
    subject: str = Field(min_length=1,max_length=500)
    predicate: Literal['analyzed_by','recommends_check','may_affect','depends_on','defines','compares_with','related_to']
    object: str = Field(min_length=1,max_length=500)
    modality: Literal['asserted','possible','recommended','conditional']
    conditions: list[BoundedCondition] = Field(default_factory=list,max_length=16)
    status: Literal['candidate'] = 'candidate'

    @classmethod
    def from_claim(cls,claim:DocumentClaim):
        if claim.status!='candidate':raise LightGraphError('claim_cache_requires_candidate_output')
        return cls(subject=claim.subject,predicate=claim.predicate,object=claim.object,
                   modality=claim.modality,conditions=list(claim.conditions))

class CachedSection(StrictModel):
    cache_key: str = Field(pattern=r'^[0-9a-f]{64}$')
    document_id: str = Field(min_length=1,max_length=1024)
    section_id: str = Field(min_length=1,max_length=1024)
    purpose: Purpose
    scope: DocumentScope
    section_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    ancestor_context_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    operator_version: Literal['document_claim_extraction/2','document_claim_extraction/3'] = OPERATOR_VERSION
    extractor_fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')
    context_fingerprint: str = Field(default_factory=lambda:digest('legacy_document_context'),pattern=r'^[0-9a-f]{64}$')
    complete: Literal[True] = True
    claims: list[CachedClaim] = Field(default_factory=list,max_length=32)

class ClaimCache(StrictModel):
    schema_version: Literal['1.0'] = '1.0'
    entries: dict[str,CachedSection] = Field(default_factory=dict,max_length=4096)
    @model_validator(mode='after')
    def matching_keys(self):
        if any(k!=v.cache_key for k,v in self.entries.items()):raise ValueError('claim_cache_key_mismatch')
        return self

class ClaimExtractionResult(StrictModel):
    claims: list[DocumentClaim]
    cache: ClaimCache
    reused_sections: int = 0
    extracted_sections: int = 0
    complete: Literal[True] = True


def extractor_fingerprint(model=None) -> str:
    """Stable extraction dependency for both document and section cache keys.

    The operator version plus configured model/endpoint define the semantics;
    Python wrapper class names, credentials and request bodies do not. A caller
    without a configured model name retains the deterministic identity (including
    cache-hit test sentinels which deliberately must never receive a request).
    """
    config=getattr(model,'config',None)
    name=getattr(config,'model',None)
    # No credentials, request bodies or provider response hashes enter the key.
    if not name:
        return digest(OPERATOR_VERSION,'deterministic')
    endpoint=getattr(config,'base_url',None)
    return digest(OPERATOR_VERSION,'configured_model',name,
                  endpoint.rstrip('/') if isinstance(endpoint,str) else endpoint)


def _entry_description(document,section,model,goal=None,subtask=None):
    ancestors=[s for s in document.sections if len(s.heading_path)<len(section.heading_path)
               and section.heading_path[:len(s.heading_path)]==s.heading_path]
    # Whole ancestor sections conservatively cover conditions/units/exceptions;
    # line numbers and whole-document revision deliberately do not invalidate
    # unchanged sections when another sibling changes.
    # The inferred document title comes from the first heading, which can change
    # when a sibling is removed. It is not an extraction input; heading paths are
    # already included in each section's identity.
    context_hash=digest(document.derived,document.dependency_refs,
                        [(s.heading_path,s.content_hash,s.purpose,s.scope.model_dump() if s.scope else None) for s in ancestors])
    extractor=extractor_fingerprint(model)
    call_context=digest(goal_payload(goal),'T4_document',subtask or STAGE_SUBTASKS['T4_document'],CONTEXT_SELECTION_VERSION)
    purpose=section.purpose or document.purpose
    scope=section.scope or document.scope
    key=digest(document.document_id,purpose,scope.model_dump(),
               section.section_id,section.content_hash,context_hash,OPERATOR_VERSION,extractor,call_context)
    return CachedSection(cache_key=key,document_id=document.document_id,section_id=section.section_id,
        purpose=purpose,scope=scope.model_copy(deep=True),section_hash=section.content_hash,
        ancestor_context_hash=context_hash,extractor_fingerprint=extractor,context_fingerprint=call_context)


def _restore(claim,document,section,known_entities):
    alignment=ClaimAlignment()
    if known_entities:
        matches=known_entities.get(claim.subject,[])
        if len(matches)==1:alignment=ClaimAlignment(entity_id=matches[0],status='candidate')
    return DocumentClaim(claim_id='claim_'+digest(document.document_id,section.section_id,claim.subject,
        claim.predicate,claim.object,claim.modality,claim.conditions)[:24],subject=claim.subject,
        predicate=claim.predicate,object=claim.object,modality=claim.modality,conditions=list(claim.conditions),
        document=DocumentReference(document_id=document.document_id,revision=document.revision,
            section_id=section.section_id,locator=section.locator),alignment=alignment,
        status='candidate',evidence_text='',scope=(section.scope or document.scope).model_copy(deep=True))


def extract_with_cache(document:KnowledgeDocument,model=None,*,known_entities=None,cached_sections=None,
                       goal=None,subtask=None) -> ClaimExtractionResult:
    """Return a replacement cache only after every current section is complete.

    Reuse requires unchanged purpose, scope, local content, ancestor context and
    extraction operator. Rehydrated claims point at the current document revision
    and locator; entity alignment is performed again against the current graph.
    Removed sections are absent from the replacement cache.
    """
    if cached_sections is None:previous={}
    elif isinstance(cached_sections,ClaimCache):previous=cached_sections.entries
    elif isinstance(cached_sections,dict) and all(isinstance(v,CachedSection) and k==v.cache_key for k,v in cached_sections.items()):
        previous=cached_sections
    else:raise LightGraphError('typed_claim_cache_required')
    if document.purpose=='unresolved':raise LightGraphError('document_extract_incomplete')
    if document.purpose=='data_description':return ClaimExtractionResult(claims=[],cache=ClaimCache())
    claims=[];entries={};reused=extracted=0
    for section in document.sections:
        description=_entry_description(document,section,model,goal,subtask)
        existing=previous.get(description.cache_key)
        if existing is not None:
            # Recompute and compare each dependency, not just a user-supplied key.
            if existing.model_dump(exclude={'claims'})!=description.model_dump(exclude={'claims'}):
                raise LightGraphError('claim_cache_dependency_mismatch')
            current=[_restore(c,document,section,known_entities) for c in existing.claims]
            entries[description.cache_key]=existing.model_copy(deep=True)
            reused+=1
        else:
            current=_extract_uncached(document,model,known_entities=known_entities,_section_ids={section.section_id},
                                      goal=goal,subtask=subtask)
            description.claims=[CachedClaim.from_claim(c) for c in current]
            entries[description.cache_key]=description
            extracted+=1
        claims.extend(current)
    if not claims and document.sections and goal is None:raise LightGraphError('document_extract_incomplete')
    return ClaimExtractionResult(claims=list({c.claim_id:c for c in claims}.values()),
        cache=ClaimCache(entries=entries),reused_sections=reused,extracted_sections=extracted)
