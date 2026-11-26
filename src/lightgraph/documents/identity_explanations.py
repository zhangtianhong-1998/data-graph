"""Source-bound identity interpretations of ordinary, versioned business text."""
import re

from pydantic import Field

from lightgraph.contracts import StrictModel, digest


class IdentityExplanation(StrictModel):
    evidence_ref: str = Field(min_length=1, max_length=512)
    quote: str = Field(min_length=1, max_length=8000,
        description='An exact, continuous quotation from the fully read applicable document section.')
    source_id: str = Field(min_length=1)
    schema_name: str = Field(default='public', alias='schema')
    table: str = Field(min_length=1)
    key_columns: list[str] = Field(min_length=1, max_length=32)
    key_roles: list[str] = Field(min_length=1, max_length=32,
        description='Ordered semantic key roles explicitly named in the quotation, including every identity qualifier.')
    version: str | None = Field(default=None, min_length=1, max_length=200,
        description='The identifier-system edition stated in the text, if any; not the document file revision.')


_LOCAL = re.compile(
    r'(?:各自|独立|本地|分别|自管|自行|单独).{0,18}(?:编号|编码|标识|号码)|'
    r'(?:编号|编码|标识|号码).{0,18}(?:各自|独立|本地|自管|自行|单独)|'
    r'仅在.{0,24}唯一|(?:不|不能|不可|并非|无法).{0,12}(?:同一|相同|共享|合并|唯一)|'
    r'\b(?:independent|local|own)\s+(?:identifiers?|codes?|numbers?|numbering)\b|'
    r'\b(?:not|never)\b.{0,32}\b(?:same|shared|unique)\b', re.I)
_SHARED = re.compile(
    r'(?:全局|跨表|跨来源|跨系统|全企业|全集团|全公司|所有系统|各系统).{0,24}(?:唯一|同一|相同)|'
    r'(?:统一|共同|共享).{0,24}(?:唯一|同一对象|同一实体)|'
    r'(?:同码|相同编码|相同编号|相同标识).{0,12}(?:表示|代表|指向|对应|标识)同一|'
    r'\bglobally\s+unique\b|\bunique\s+across\b|'
    r'\b(?:same|matching)\s+(?:codes?|identifiers?)\b.{0,40}\b(?:same|one)\b', re.I)
_VERSION = re.compile(r'版本|版次|\b(?:version|edition|revision)\b', re.I)
_SHARED_SYSTEM = re.compile(
    r'(?:跨表|跨来源|跨系统|各系统|所有系统|不同来源|不同系统).{0,24}'
    r'(?:共享|共用|统一|同一).{0,24}(?:编号体系|编码体系|标识体系|编号|编码|标识)|'
    r'(?:共享|共用|统一|同一).{0,24}(?:编号体系|编码体系|标识体系).{0,24}'
    r'(?:跨表|跨来源|跨系统|各系统|所有系统|不同来源|不同系统)|'
    r'\b(?:all|across)\s+(?:source\s+)?systems\b.{0,40}\b(?:shared|common|same)\b.{0,24}'
    r'\b(?:identifier|numbering|code)\s+(?:system|namespace)\b', re.I)
_QUALIFIED = re.compile(r'除.{0,30}外|但是|仅在|只在|限于|前提|条件|例外|可能|通常|'
    r'尚未|尚不|未(?:确认|验证|核实|保证|确保|证明|统一|实现)|待(?:确认|验证|核实)|'
    r'是否|不清楚|不确定|目标|要求|期望|预计|计划|应当|应该|需要|必须|'
    r'应(?:在|为|保证|确保)|拟(?:在|将|统一|使用)|例如|比如|举例|假设|假如|如果|或许|'
    r'\b(?:except|unless|only\s+within|conditional|may|usually|unconfirmed|unverified|'
    r'unknown|pending|expected|planned|should|must|requirement|goal|example|hypothetical|if)\b', re.I)


def _asserts_domain(quote, domain):
    return any(domain in clause and _SHARED.search(clause) and _SHARED_SYSTEM.search(clause)
               for clause in re.split(r'[。；;\n]', quote))


def _explicit_role_binding(quote, column, role):
    physical_name = r'(?<![a-z0-9_])' + re.escape(column) + r'(?![a-z0-9_])'
    if column == role:
        return bool(re.search(physical_name, quote, re.I))
    return bool(re.search(physical_name + r'[^。；;\n]{0,80}'
                          r'(?:对应|表示|记录|引用|标识|存放|就是|\bis\b|\bcontains\b|\bholds\b)'
                          + r'[^。；;\n]{0,40}' + re.escape(role), quote, re.I))


def explanation_claims(entity, mapping, table, applicable, cited):
    """Verify a proposed interpretation against original text and independent restrictions.

    A model supplies field binding and an exact quote, never a trusted marker.
    Unknown wording remains reviewable; it does not acquire identity authority.
    """
    from .identity_contract import _concerns_authored_key

    address = (mapping.source_id, mapping.schema_name, mapping.table)
    all_sections = {ref: section for group in applicable for ref, section in group.items()}
    common = set.intersection(*(set(group) for group in applicable)) if applicable else set()
    matching = [e for e in entity.identity_explanations
                if (e.source_id, e.schema_name, e.table) == address and e.key_columns == mapping.key_columns]
    issues, claims = [], []
    for proposed in matching:
        ref = proposed.evidence_ref
        section = all_sections.get(ref)
        reason = None
        if ref not in cited or ref not in common or section is None:
            reason = 'identity_explanation_reference_not_applicable'
        elif proposed.quote not in section['text']:
            reason = 'identity_explanation_quote_mismatch'
        elif (not entity.identity_domain or entity.identity_domain not in proposed.quote
                or len(proposed.key_roles) != len(mapping.key_columns)
                or len(set(proposed.key_roles)) != len(proposed.key_roles)
                or any(term not in proposed.quote for term in (*mapping.key_columns, *proposed.key_roles))):
            reason = 'identity_explanation_key_binding_incomplete'
        elif not all(_explicit_role_binding(proposed.quote, column, role)
                     for column, role in zip(mapping.key_columns, proposed.key_roles, strict=True)):
            reason = 'identity_explanation_key_roles_unverified'
        elif _LOCAL.search(proposed.quote):
            reason = 'identity_material_conflict'
        elif not _asserts_domain(proposed.quote, entity.identity_domain) or _QUALIFIED.search(proposed.quote):
            reason = 'identity_explanation_requires_semantic_review'
        elif ((proposed.version and proposed.version not in proposed.quote)
              or (_VERSION.search(proposed.quote) and not proposed.version)):
            reason = 'identity_explanation_version_unverified'
        if not reason:
            for other in all_sections.values():
                relevant = _concerns_authored_key(other, (*entity.key_names, *mapping.key_columns,
                                                          entity.identity_domain), table)
                if relevant and _LOCAL.search(other['text']):
                    reason = 'identity_material_conflict'
                    break
                if relevant and _QUALIFIED.search(other['text']):
                    reason = 'identity_material_requires_semantic_review'
                    break
                for binding in other.get('identity_bindings', ()):
                    if (binding['table'] == f'{address[0]}:{address[1]}.{address[2]}'
                            and set(binding['key_columns']).intersection(mapping.key_columns)
                            and (binding.get('incomplete') or binding.get('scope') != 'shared'
                                 or binding.get('domain') != entity.identity_domain
                                 or binding['key_columns'] != mapping.key_columns)):
                        reason = 'identity_material_conflict'
                        break
        if reason:
            issues.append({'code': reason, 'evidence_ref': ref})
            continue
        claims.append({'evidence_ref': ref, 'document_id': section['doc_id'],
                       'document_revision': section['revision'], 'section_id': section['section_id'],
                       'quote': proposed.quote, 'quote_digest': digest(proposed.quote),
                       'domain': entity.identity_domain, 'key_columns': list(mapping.key_columns),
                       'key_roles': list(proposed.key_roles), 'version': proposed.version})
    # One positively interpreted paragraph cannot override another conflicting interpretation.
    return ([] if issues else claims), issues
