"""Author-issued identity contracts in versioned data dictionaries."""
import re
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from lightgraph.contracts import StrictModel


Identifier = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=256)]


class RegistryReference(StrictModel):
    issuer: Identifier
    catalog: Identifier
    version: Identifier
    key_roles: list[Identifier] = Field(min_length=1, max_length=64)


class DocumentIdentityContract(StrictModel):
    domain: str = Field(min_length=1, max_length=512)
    scope: Literal['shared', 'source_local']
    key_columns: dict[str, list[str]] = Field(min_length=1, max_length=512)
    registry: RegistryReference | None = None

    @field_validator('key_columns')
    @classmethod
    def qualified_keys(cls, bindings):
        for address, key in bindings.items():
            if (not re.fullmatch(r'[^:\s]+:[^.:\s]+\.[^:\s]+', address)
                    or not key or len(key) != len(set(key)) or any(not col.strip() for col in key)):
                raise ValueError('An identity contract must bind complete ordered keys to source:schema.table.')
        return bindings

    @model_validator(mode='after')
    def complete_registry_key(self):
        if self.registry:
            roles = self.registry.key_roles
            if (self.scope != 'shared' or len(set(roles)) != len(roles)
                    or any(len(key) != len(roles) for key in self.key_columns.values())):
                raise ValueError('Each complete shared key must bind one distinct registry role per column.')
        return self


def _sentence(section):
    # This legacy form accepts one unqualified statement. Extra sentences,
    # conditions, lists and prose about other identifiers need an authored contract.
    text = re.sub(r'^#{1,6}[^\n]*(?:\n|$)', '', section['text'], count=1)
    return ' '.join(text.split()).removesuffix('.')


def _plain_declaration(sentence, domain, name, scoped_field):
    """Recognize complete unconditional uniqueness declarations for one named key."""
    issuer = re.escape(domain)
    if scoped_field:
        issuer = rf'(?:{issuer}|the registry)'
    noun = r'[\w-]+'
    key = re.escape(name)
    declaration = (
        rf'(?:{issuer} assigns each {noun} exactly one {key}, unique across (?:its|all|both) {noun}'
        rf'|{issuer} issues one globally unique (?:{noun} )?{key} to (?:all|both) {noun}'
        rf'|(?:version [\d.]+: )?{key} identifies one {noun} across (?:all|both) {noun})'
    )
    # A bare statement about "code" only denotes this physical key when the
    # document itself restricts that section to that column.
    if not scoped_field and not re.match(rf'{re.escape(domain)} ', sentence, re.I):
        return False
    return re.fullmatch(declaration, sentence, re.I) is not None


def _field_reference(sentence, col, name, domain):
    # In a table-only section an alias must be explicitly assigned to the
    # named registry and logical key. Mentioning these three tokens is insufficient.
    return bool(re.fullmatch(rf'(?:the )?{re.escape(col)} column (?:holds|contains) the full '
                             rf'{re.escape(domain)} [\w-]+ {re.escape(name)}', sentence, re.I))


def _field_note(sentence, section, table, col, domain):
    """A scoped field/table description can accompany, but never authorize, a claim."""
    if (not section['scope'].get('tables') or section['scope'].get('columns') != [col]
            or not re.fullmatch(r'[\w -]+', sentence)):
        return False
    words = lambda value: set(re.findall(r'[^\W_]+', value.casefold()))
    vocabulary = words(' '.join((domain, table.name, *table.column_names)))
    # Only noun labels and simple field usages are neutral. Unknown predicates,
    # quantifiers, extra clauses or prose in another language go to review.
    vocabulary.update('the a an this field column local identifier in of at listing table records identifies'.split())
    content = words(sentence)
    return bool(words(col) <= content and words(table.name) <= content and content <= vocabulary)


_PROSE_RESTRICTION = re.compile(
    r'(?:各自|独立|本地|分别|自管|自行|单独).{0,18}(?:编号|编码|标识|号码)|'
    r'(?:编号|编码|标识|号码).{0,18}(?:各自|独立|本地|自管|自行|单独)|'
    r'仅在.{0,24}唯一|(?:不(?!同(?:系统|来源|表|数据源|数据表))|不能|不可|并非|无法|不得).{0,12}(?:同一|相同|共享|合并|唯一)|'
    r'(?:联合|组合|共同|配合).{0,16}(?:唯一|区分|确定|标识)|'
    r'需要.{0,20}(?:区分|共同确定|联合唯一)|'
    r'除.{0,30}外|但是|只在|限于|前提|条件|例外|尚未|尚不|未(?:确认|验证|核实|保证|确保|证明|统一|实现)|'
    r'待(?:确认|验证|核实)|是否|不清楚|不确定|'
    r'\b(?:independent|local|own)\s+(?:identifiers?|codes?|numbers?|numbering)\b|'
    r'\b(?:not|never)\b.{0,32}\b(?:same|shared|share|unique)\b|'
    r'\b(?:needs?|requires?)\b.{0,40}\b(?:distinguish|uniquely\s+identify|identify|disambiguate)\b|'
    r'\b(?:except|unless|only\s+within|conditional|unconfirmed|unverified)\b',
    re.I,
)

_SCHEMA_TABLE_HEADER = re.compile(
    r'^(?:字段名?|列名?|物理列|名称|属性|类型|数据类型|字段类型|说明|中文名?|中文说明|业务含义|含义|描述|备注|是否为空|可空|主键|示例|样例|column|field|name|type|data_type|comment|description|nullable|example)$',
    re.I,
)


def _strip_neutral_schema_rows(text, table):
    """Ignore neutral Markdown schema/dictionary rows (including Chinese comments), while keeping any restriction."""
    fields = {c.casefold() for c in table.column_names}
    types = {c.data_type.casefold() for c in table.columns}
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if '|' in stripped:
            cells = [v.strip().strip('`') for v in stripped.strip('|').split('|')]
            folded = [v.casefold() for v in cells]
            if len(cells) > 1:
                if all(re.fullmatch(r':?-{2,}:?', c) or not c for c in cells):
                    continue
                if all(_SCHEMA_TABLE_HEADER.match(c) or not c for c in cells):
                    continue
                if (fields.intersection(folded) and types.intersection(folded)
                        and all(v in fields or v in types or v.isdecimal() for v in folded)):
                    continue
                if (fields.intersection(folded)
                        and (types.intersection(folded)
                             or any(re.fullmatch(r'(?:var)?char(?:\(\d+\))?|text|int(?:eger|4|8)?|bigint|smallint|numeric(?:\([\d,]+\))?|decimal(?:\([\d,]+\))?|float\d?|double|bool(?:ean)?|date|timestamp.*|string|number', v)
                                    for v in folded))
                        and not _PROSE_RESTRICTION.search(line)):
                    continue
        lines.append(line)
    return '\n'.join(lines)


def _concerns_authored_key(section, key_terms, table, *, target_columns=None):
    """Table-wide applicability does not bind every paragraph to every key.

    A neutral schema row in a field list is metadata, even with Chinese comments.
    Unscoped numbering rules can apply to every key, even without naming a column.
    This check only identifies potential contrary evidence; it cannot grant identity.
    """
    text = _strip_neutral_schema_rows(section['text'], table)
    if section['scope'].get('columns'):
        if target_columns is not None and not set(section['scope']['columns']).intersection(target_columns):
            return False
        return bool(text.strip())
    # A structured identity_contract section bound exclusively to other columns
    # on this table only concerns our key if its prose actually mentions it or
    # states a table-wide restriction.
    bindings = section.get('identity_bindings') or ()
    address = f'{table.source_id}:{table.schema_name}.{table.name}'
    if target_columns is not None and bindings:
        table_bindings = [b for b in bindings if b.get('table') == address]
        if table_bindings and all(not set(b.get('key_columns') or ()).intersection(target_columns)
                                  for b in table_bindings):
            for term in key_terms:
                if term and re.search(rf'(?<![a-z0-9_]){re.escape(term)}(?![a-z0-9_])', text, re.I):
                    return True
            return bool(_PROSE_RESTRICTION.search(text))
    for term in key_terms:
        if term and re.search(rf'(?<![a-z0-9_]){re.escape(term)}(?![a-z0-9_])', text, re.I):
            return True
    return bool(re.search(r'\b(?:numbering|identifiers?|registr(?:y|ies)|unique(?:ness)?|'
                          r'(?:own|independent|local) numbers?)\b|'
                          r'编号|标识符?|主键|唯一|各自.{0,12}(?:登记|编制|编码)|(?:独立|本地)编码', text, re.I))


def shared_key_claim_refs(domain, names, columns, table, applicable, cited):
    """Return cited sections that authorize sharing this complete physical key.

    applicability was checked against every physical column in the worker's
    authorized catalog. A source-local declaration also applies if the draft
    cited a more convenient section instead.
    """
    if not domain or len(names) != len(columns) or not applicable:
        return set()
    all_sections = {ref: section for group in applicable for ref, section in group.items()}
    common = set.intersection(*(set(group) for group in applicable))
    address = f'{table.source_id}:{table.schema_name}.{table.name}'
    proven = set()
    declared_sections = set()
    target = set(columns)
    registries = set()
    for ref, section in all_sections.items():
        for entry in section.get('identity_bindings', ()):
            if entry['table'] != address:
                continue
            declared = set(entry['key_columns'])
            if entry.get('incomplete') and target.intersection(declared):
                return set()
            if not (target <= declared or declared <= target):
                continue
            declared_sections.add(ref)
            # A declaration for (code, edition) does not license code alone.
            # In either direction the difference in grain requires a decision.
            if declared != target or entry['scope'] != 'shared' or entry['domain'] != domain:
                return set()
            registry = entry.get('registry')
            if registry:
                if entry['key_columns'] != list(columns):
                    return set()
                registries.add((registry['issuer'], registry['catalog'], registry['version'], tuple(registry['key_roles'])))
                if len(registries) > 1:
                    return set()
            if entry['key_columns'] == list(columns) and ref in common and ref in cited:
                proven.add(ref)

    legacy = {}
    for ref, section in all_sections.items():
        if ref in declared_sections:
            continue
        if proven:
            if not _concerns_authored_key(section, (*names, *columns, domain), table, target_columns=target):
                continue
            cleaned = _strip_neutral_schema_rows(section['text'], table)
            if _PROSE_RESTRICTION.search(cleaned):
                return set()
            continue
        if len(columns) != 1:
            # A second applicable prose clause cannot resolve a composite key.
            return set()
        name, col = names[0], columns[0]
        state = legacy.setdefault(section['doc_id'], {'assertion': set(), 'bound': False})
        sentence = _sentence(section)
        scope = section['scope']
        scoped_field = scope.get('columns') == [col] and bool(scope.get('tables'))
        if scope.get('tables') and _plain_declaration(sentence, domain, name, scoped_field):
            if ref in cited and ref in common:
                state['assertion'].add(ref)
                state['bound'] |= scoped_field
        elif _field_reference(sentence, col, name, domain):
            state['bound'] |= ref in cited
        elif not _field_note(sentence, section, table, col, domain):
            # Includes uncited restrictions in this or another applicable
            # dictionary. Citing just the strongest section cannot hide them.
            return set()
    for entry in legacy.values():
        if entry['bound']:
            proven.update(entry['assertion'])
    return proven
