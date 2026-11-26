"""Ground a question in the active, authorized graph before choosing a traversal."""
from __future__ import annotations

import re

from lightgraph.contracts import canonical

from lightgraph.construction.contexts import normalize_context_selection
from .retriever import decompose_query, is_literal_span
from .semantic_grounding import link_mentions
from .store_sqlite import normalize_term

_ACTION = re.compile(r'下钻|逐层|钻取|下级|子节点|子类|下一层|最底层|明细层|drill\s*down', re.I)
_POINT_ONLY = re.compile(r'(?:只|仅)(?:查看?|看|返回|显示|需要)(?:(?:这|该|此)(?:一个|个)?|单个|一个)?节点(?:本身|详情)?|仅此节点')
_OVERVIEW = re.compile(
    r'(?:全图|整[个张]图|图谱|全局)(?:的|有哪些)?(?:概览|概况|社区|社群|中心度)|'
    r'(?:核心|关键)节点|(?:哪些节点的?)?(?:入度|出度|中心度)最高(?:的节点)?|'
    r'(?:有哪些社区|社区(?:分布|概览|划分))|\bpagerank\b', re.I)
_EXPLORE = re.compile(r'相关|有哪些关联|邻居|探索|附近|周边|关联关系|explore', re.I)
_PATH_OPERATION = re.compile(r'多跳|(?:多层(?:级)?|逐层)关联|路径|(?:关联|关系)链|如何(?:关联|连接|连通)|之间(?:的|有何|有什么)?(?:关系|联系)|'
                             r'\b(?:path|paths|chain|multi[- ]hop|connected)\b', re.I)
# Offline grammar for graph operations only. Apply it to entire gaps after
# matching the actual graph vocabulary. It does not define entities or relations.
_PATH_SYNTAX = (r'之间|经过|通过|经由|之间有|之间的|如何|怎样|有何|关联|关系|连接|联系|查找|寻找|到达|通往|沿着|只沿|仅沿|'
                r'\b(?:find|show|the|a|an|between|from|to|via|and|of|are|how|connected|follow|using|only|edges?)\b')
_EDGE_DIRECTIONS = {'incoming': re.compile(r'入边|\bincoming\b', re.I),
                    'outgoing': re.compile(r'出边|\boutgoing\b', re.I),
                    'both': re.compile(r'双向|\bbidirectional\b', re.I)}
_NEG = re.compile(r'不要(?:再)?(?:看|分析|查)?|不看|排除|忽略|不考虑|不包括|不包含|不含|不关注|无需考虑|不关心|除外')
_HISTORY = re.compile(r'^(?:(?:背景|例如|举例)\s*[:：]|(?:上次|此前|之前)(?:讨论|查询|看过))')
_FOCUS = re.compile(r'(?:本次|这次|现在)(?:请|要|只)')
_PROJECT_ALIASES = (
    {'project', '项目'}, {'project_id', '项目编号'}, {'project_name', '项目名', '项目名称'},
)
_RESTRICT = re.compile(r'限定|过滤|要求|约束|须为|必须|条件|只要|只统计|只看|仅看|'
                       r'大于|小于|超过|至多|至少|低于|高于|不超过|不少于|[<>≥≤=]|且|尚未|已经')


def _clauses(query):
    parts = re.split(r'[。；;，,！？!?\n]+', query)
    active, exclusions = [], []
    for part in parts:
        clause = part.strip()
        if _HISTORY.match(clause):
            focus = _FOCUS.search(clause)
            if not focus:
                continue
            clause = clause[focus.start():]
        if clause:
            # Negation is a clause operator. Never cut arbitrary nouns at
            # an interior substring that happens to resemble a negation.
            (exclusions if _NEG.match(clause) else active).append(clause)
    return active, exclusions


def _dimension_aliases(name, known):
    name = normalize_term(name)
    group = next((g for g in _PROJECT_ALIASES if name in g), {name})
    aliases = {name, *(a for a in group if a not in known)}
    if group in _PROJECT_ALIASES and len(known.intersection(set.union(*_PROJECT_ALIASES))) == 1:
        aliases.update(_PROJECT_ALIASES[0])
    return aliases


def _dimension_pattern(name, known):
    return '(?:' + '|'.join(re.escape(n) for n in sorted(_dimension_aliases(name, known), key=len, reverse=True)) + ')'


def _scope_matches(text, authorized):
    names = {normalize_term(key) for key, _ in authorized}
    patterns = {name: _dimension_pattern(name, names) for name in names}
    for name, value in authorized:
        escaped = re.escape(str(value))
        key = patterns[normalize_term(name)]
        # Include a boundary for Latin identifiers so "M" cannot select "AM".
        pattern = rf'(?P<value_first>(?<![a-zA-Z0-9_]){escaped}\s*{key}(?![a-zA-Z0-9_]))|(?<![a-zA-Z0-9_]){key}\s*[=:：]?\s*{escaped}(?![a-zA-Z0-9_])'
        for match in re.finditer(pattern, text, re.I):
            # A registered short value can be the suffix or prefix of an
            # unrelated name (e.g. 京 and 南京). An unsegmented value needs
            # a clause boundary or an explicit scope operator.
            if not str(value).isascii():
                if match['value_first'] and match.start() and text[match.start() - 1].isalnum():
                    lead = re.split(r'[\s,，。；;:：]', text[:match.start()])[-1]
                    if not (_NEG.fullmatch(lead) or re.fullmatch(r'(?:在|只看|仅看|查询|查看|限定|分析)(?:在)?', lead)):
                        continue
                if not match['value_first'] and match.end() < len(text) and text[match.end()].isalnum():
                    continue
            yield name, value, match


def _mask_scopes(text, matches):
    # Keep every position outside an actual authorized name/value match. A
    # string replacement for "M 项目" could also erase the suffix of "AM 项目".
    chars = list(text)
    for _, _, match in matches:
        start, end = match.span()
        chars[start:end] = [' '] * (end - start)
    return ''.join(chars)


def _endpoint_role(prefix):
    prefix = prefix.rstrip()
    if re.search(r'(?:从|\bfrom)$', prefix, re.I):
        return 'start'
    if re.search(r'(?:到|至|通往|\bto)$', prefix, re.I):
        return 'end'
    if re.search(r'(?:经过|经由|\bvia)$', prefix, re.I):
        return 'via'
    return None


def _scope_scan_text(retriever, scopes, selection, text, dimensions, is_path):
    """Hide indexed endpoints from the scope parser, preserving source offsets."""
    if not dimensions or not (is_path or _PATH_OPERATION.search(text)):
        return text
    names = {normalize_term(name) for name, _ in dimensions}
    keys = {_dimension_pattern(name, names) for name in names}
    if not any(re.search(key, text, re.I) for key in keys):
        return text
    cohorts = retriever.authorized_cohorts(scopes, selection)
    terms = retriever.index.terms_in_text(text, cohorts)
    result = list(text)
    for term in decompose_query(text, known_terms=terms):
        if term.source != 'exact_term' or not any(re.search(key, term.surface, re.I) for key in keys):
            continue
        for m in re.finditer(re.escape(term.surface), text, re.I):
            if is_literal_span(text, *m.span()) and _endpoint_role(text[:m.start()]):
                result[m.start():m.end()] = ['，'] * len(m.group())
    return ''.join(result)


def _resolve_scope(retriever, scopes, requested, clauses, excluded, *, is_path=False):
    selection = normalize_context_selection(requested)
    dimensions = set()
    for token in retriever.authorized_cohorts(scopes, None):
        dimensions.update(retriever.cohort_contexts[token]['dimensions'].items())
    text = '，'.join(clauses)
    unresolved = []
    scan = _scope_scan_text(retriever, scopes, selection, text, dimensions, is_path)
    matches = list(_scope_matches(scan, dimensions))
    if matches and selection == {}:
        return selection, text, ['已选择仅通用知识，请确认问题中的专属范围。']
    for name, value, _ in matches:
        selection = dict(selection or {})
        if name in selection and canonical(selection[name]) != canonical(value):
            return normalize_context_selection(requested), text, ['出现互不一致的适用范围。']
        selection[name] = value
    text = _mask_scopes(text, matches)
    scan = _mask_scopes(scan, matches)
    names = {normalize_term(name) for name, _ in dimensions}
    # Detect an unmatched qualifier using dimension names actually present
    # in this index. Bare words in another domain are not scope declarations.
    if any(re.search(rf'(?<![a-zA-Z0-9_]){key}\s*(?:[=:：]\s*\S|[a-z\d_-]+)|'
                     rf'[\w-]+\s*{key}(?=的|中|里|查|看|\s|[，,。]|$)', scan, re.I)
           for key in ({_dimension_pattern(name, names) for name in names}
                                | (_PROJECT_ALIASES[0] if names & set.union(*_PROJECT_ALIASES) else set()))):
        unresolved.append('所提范围无法在当前可见资料中确认。')
    for clause in excluded:
        matches = list(_scope_matches(clause, dimensions))
        residual = _mask_scopes(clause, matches)
        neg = _NEG.match(residual)
        if (not matches or not all(key in (selection or {}) and selection[key] != val for key, val, _ in matches)
                or not neg or not re.fullmatch(r'[\s的中和与及、]*', residual[neg.end():])):
            unresolved.append('排除条件尚未落实，请明确需要保留的范围。')
    return selection, text, unresolved


def _resolve_text(index, text, cohorts, kind=None):
    hits = index.exact_names([text], scopes=cohorts, limit_per_term=6, include_codes=True, kind=kind)
    exact = hits.get(normalize_term(text), [])
    return exact[:5], len(exact) > 5


# Offline compatibility grammar for the full gap BETWEEN recognized spans.
# Never substitute these words inside the text of a prospective entity.
_GAP_SYNTAX = (
    r'\s+|[的与和及在按沿到至从把中内下再并各做对了向里请查看]|'
    r'请问|帮我|我想(?:知道|了解)|本次|这次|现在|只看|仅看|查询|分析|'
    r'看看|节点|本身|详情|返回|同时满足|这个|这(?:一块|部分)|'
    r'究竟|分别|对应|哪(?:些|几种|几个)|有哪(?:些|几种|几个)|是什么|'
    r'拆开|细分|展开|分组|数据|结果|内容|共同记录|同时出现|交集'
)
_CLAUSE_BOUNDARY = re.compile(r'[，,。；;、：:“”「」（）()？?!！\n]+')
_POLITE_REQUEST = re.compile(r'^(?:请(?:问|查(?:询)?|查看)|帮我(?:查询|查看))\s+')
_LEVEL = re.compile(r'(?:下钻|钻取|展开)(?:一直)?(?:到|至)\s*([^，。\s]+)')
# Similarity can link an entity name. It cannot enforce a comparison, a time
# interval, an exclusion or a logical operator on that entity's observations.
_PREDICATE = re.compile(
    r'大于|小于|高于|低于|超过|至少|最多|至多|不少于|不超过|[<>≤≥=]|'
    r'排除|仅限|限定|只统计|除外|不含|不包括|尚未|已经|而非|或者|相比|对比|'
    r'去年|今年|上个月|上月|去年同期|最近\s*[\d一二三四五六七八九十几]+|\d+\s*(?:年|月|天|周|元|%)|'
    r'\b(?:before|after|without|except|excluding|greater than|less than|at least|at most)\b', re.I)


def _residuals(text, slots, level, framing=(), *, path=False):
    text = normalize_term(text)
    anchors = {normalize_term(s['text']) for s in slots} | {normalize_term(f) for f in framing}
    anchors.discard('')
    names = '|'.join(re.escape(s) for s in sorted(anchors, key=len, reverse=True))
    # Consume the possessive only at a recognized left operand. In isolation
    # an unknown name stays intact, including its first character.
    if names:
        def mask(match):
            return '\n' if is_literal_span(text, *match.span('name')) else match.group()
        text = re.sub(r'(?P<name>' + names + r')(?:的)?', mask, text)
    kinds = {normalize_term(s['role']) for s in slots if s.get('role')}
    if level:
        kinds.add(normalize_term(level))
    words = [_GAP_SYNTAX, _ACTION.pattern, _EXPLORE.pattern, _POINT_ONLY.pattern]
    if path:
        words.extend((_PATH_OPERATION.pattern, _PATH_SYNTAX))
        words.extend(pattern.pattern for pattern in _EDGE_DIRECTIONS.values())
    words.extend(re.escape(k) for k in kinds if k)
    grammar = re.compile(r'(?:' + '|'.join(words) + r')+', re.I)
    fragments = []
    for raw in _CLAUSE_BOUNDARY.split(text):
        part = raw.strip()
        if not part or grammar.fullmatch(part):
            continue
        part = _POLITE_REQUEST.sub('', part, count=1)
        if part:
            fragments.append(part)
    return list(dict.fromkeys(fragments))


def _lexical_suggestions(retriever, scopes, selection, fragments):
    # In a clarification, even low confidence lexical hits can be useful.
    fragments = fragments[:5]
    opts = dict(scopes=scopes, business_context=selection, hops=0, top_n=5, top_p_seeds=3,
                extractor=lambda _: [{'surface': text} for text in fragments])
    hits = retriever.retrieve(' '.join(fragments), method='keyword', **opts)
    return [{'text': slot['surface'], 'candidates': slot['candidates'], 'status': 'suggestion'}
            for slot in hits['slots']]


def _target_kind(text, kinds):
    kinds = sorted(kinds, key=len, reverse=True)
    for kind in kinds:
        k = re.escape(kind)
        # A type becomes the requested output grain only through a grammatical
        # target in the question.
        if re.search(rf'(?:哪些|哪几种|哪几个|细分到|展开到|下钻到|钻取到|下钻至)\s*{k}|'
                     rf'(?:按|依照|根据)\s*{k}\s*(?:拆|展开|细分|分解|分组|分别|列)|'
                     rf'(?:对应的?|包括的?)\s*{k}\s*(?:都有|有哪些|分别|是什么|有哪)', text, re.I):
            return kind
    return None


def _parent_roles(index, cohorts, slots, target_kind):
    parents = {}
    for (src, _, dst), direction in index.authorized_hierarchies(cohorts).items():
        if direction:
            parent, child = (src, dst) if direction == 'outgoing' else (dst, src)
            parents.setdefault(child, set()).add(parent)
    frontier, seen = {target_kind}, {target_kind}
    while frontier:
        frontier = {p for k in frontier for p in parents.get(k, ())} - seen
        if not frontier:
            break
        roles = {s['role'] for s in slots if s['role'] and any(c['kind'] in frontier for c in s['candidates'])}
        if roles:
            return roles
        seen.update(frontier)
    return set()


def _combine_mentions(focus, literal, proposed):
    """Prefer the whole mention when the model and index split the same span differently."""
    if not proposed:
        return literal
    text = normalize_term(focus)
    phrases = {normalize_term(s): s for s in literal}
    literal_terms = set(phrases)
    for span in proposed:
        term = normalize_term(span)
        # A parser can join multiple conditions into one long noun phrase.
        # A close vector for that phrase must not erase two known operands.
        if term in literal_terms or sum(t in term for t in literal_terms) < 2:
            phrases[term] = span
    occupied = bytearray(len(text))
    selected = []
    for phrase in sorted(phrases, key=lambda p: (-len(p), p)):
        if not phrase:
            continue
        for hit in re.finditer(re.escape(phrase), text):
            start, end = hit.span()
            if not any(occupied[start:end]):
                occupied[start:end] = b'\x01' * (end - start)
                selected.append((start, phrases[phrase]))
    return list(dict.fromkeys(s for _, s in sorted(selected)))


def _bind_exact(retriever, cohorts, plan, surfaces):
    """Return unmatched spans; candidate node types always come from the visible graph."""
    pending = []
    for surface in surfaces:
        candidates, capped = _resolve_text(retriever.index, surface, cohorts)
        if candidates:
            kinds = {c['kind'] for c in candidates}
            plan['conditions'].append({'text': surface, 'role': next(iter(kinds)) if len(kinds) == 1 else None,
                                       'match': 'exact', 'candidates': candidates})
            plan['candidate_limit'] |= capped
        else:
            pending.append(surface)
    return pending


def _link_open_terms(retriever, cohorts, plan, phrases, equivalents=None):
    phrases = list(dict.fromkeys(phrases))
    room = 12 - len(plan['conditions'])
    if len(phrases) > room:
        plan['candidate_limit'] = True
        plan['unresolved'].append('条件数量超出本次联合查询范围。')
    for link in link_mentions(retriever, phrases[:max(0, room)], cohorts, paraphrases=equivalents):
        surface = link['text']
        if link['status'] == 'linked':
            best = link['candidates'][0]
            plan['conditions'].append({'text': surface, 'role': best['kind'], 'match': 'semantic',
                                       'candidates': [best]})
            continue
        plan['unbound_mentions'].append({'text': surface, 'reason': link['status']})
        if link['candidates']:
            plan['suggestions'].append({**link, 'status': 'suggestion', 'reason': link['status']})
        plan['unresolved'].append('当前可见图谱中未能确认“' + surface + '”对应的实体。')


def _ground_index_query(retriever, cohorts, focus, terms, plan, *, path=False):
    """Limited fallback for installations without a working question parser."""
    literal = [s.surface for s in decompose_query(focus, known_terms=terms) if s.source == 'exact_term']
    if len(literal) > 12:
        plan['candidate_limit'] = True
        plan['unresolved'].append('条件数量超出本次联合查询范围。')
    _bind_exact(retriever, cohorts, plan, literal[:12])
    residual = _residuals(_OVERVIEW.sub(' ', focus), plan['conditions'], plan['requested_level'], path=path)
    eligible = [s for s in residual if len(s) > 1 and not _PREDICATE.search(s)
                and not _RESTRICT.search(s) and not _NEG.search(s) and not s.isnumeric()]
    _link_open_terms(retriever, cohorts, plan, eligible)


def _new_plan(selection, focus, parser):
    return {'intent': 'search', 'business_context': selection, 'conditions': [], 'drill_role': None,
            'unresolved': [], 'unbound_mentions': [], 'unverified_predicates': [], 'suggestions': [], 'point_only': False,
            'requested_level': None, 'excluded_terms': [], 'focus': focus.strip(), 'candidate_limit': False,
            'parser': parser}


def _model_scope(retriever, scopes, requested, query, parsed, *, is_path=False):
    selection = normalize_context_selection(requested)
    available = set()
    for token in retriever.authorized_cohorts(scopes, None):
        available.update(retriever.cohort_contexts[token]['dimensions'].items())
    excluded = tuple(dict.fromkeys(parsed.get('exclusions', ())))
    asides = set(parsed.get('background', ())).union(excluded)
    masked = list(query)
    for span in asides:
        # The parser must quote a complete background/exclusion. Removing an
        # ambiguous occurrence or a suffix of a project name can widen scope.
        hits = list(re.finditer(re.escape(span), query)) if span else []
        if len(hits) != 1:
            return selection, '', ['无法区分背景、排除范围与当前问题中的同一表述。']
        start, end = hits[0].span()
        if (not is_literal_span(query, start, end)
                or (start and query[start - 1].isalnum() and query[start].isalnum())
                or (end < len(query) and query[end - 1].isalnum() and query[end].isalnum())):
            return selection, '', ['背景或排除条件未覆盖完整表述。']
        masked[start:end] = ['\n'] * (end - start)

    focus = ''.join(masked)
    scan = _scope_scan_text(retriever, scopes, selection, focus, available, is_path)
    visible_names = {normalize_term(name) for name, _ in available}
    for scope in parsed.get('scopes', ()):
        phrase = scope['text']
        dimension, value = scope['dimension'], scope['value']
        if not phrase or not dimension or not value or dimension not in phrase or value not in phrase:
            return selection, '', ['范围名称、维度与原文不一致。']
        occurrences = list(re.finditer(re.escape(phrase), focus))
        if occurrences:
            slices = {scan[m.start():m.end()] for m in occurrences}
            if slices == {'，' * len(phrase)}:
                # The quoted scope is itself a named, authorized path operand.
                continue
            if slices - {phrase, '，' * len(phrase)}:
                return selection, '', ['范围限定与路径中的对象重叠，无法分别确认。']
        keys = [(key, v) for key, v in available if normalize_term(v) == normalize_term(value) and
                normalize_term(dimension) in _dimension_aliases(key, visible_names)]
        if len(keys) != 1:
            return selection, '', ['所提范围无法与当前可见的适用范围唯一对应。']
        key, v = keys[0]
        if selection == {} or key in (selection or {}) and canonical(selection[key]) != canonical(v):
            return normalize_context_selection(requested), '', ['出现互不一致的适用范围。']
        # Align against active text, not just the quoted fragment. "京项目" is
        # well formed in isolation even when copied from inside "南京项目".
        phrases = list(re.finditer(re.escape(phrase), scan))
        if not phrases:
            return selection, '', ['所提范围只出现在背景、排除条件中或未在原文找到。']
        adjacent = list(_scope_matches(phrase, keys))
        full = [hit for _, _, hit in _scope_matches(scan, keys)]
        for occurrence in phrases:
            start, end = occurrence.span()
            if not is_literal_span(focus, start, end):
                return selection, '', ['所提范围与原文中的完整名称不一致。']
            if adjacent:
                fragments = [hit.span() for hit in full if start <= hit.start() and hit.end() <= end]
                if len(fragments) != len(adjacent):
                    return selection, '', ['所提范围与原文中的完整名称不一致。']
            else:
                # Free-form wording can separate the dimension and the value.
                # Mask just these two literals. Leave all qualifiers and
                # predicates in the question for independent interpretation.
                fragments = []
                for atom in (dimension, value):
                    hits = list(re.finditer(re.escape(atom), phrase))
                    if len(hits) != 1:
                        return selection, '', ['无法唯一定位范围的名称与维度。']
                    left, right = start + hits[0].start(), start + hits[0].end()
                    if not is_literal_span(focus, left, right):
                        return selection, '', ['所提范围与原文中的完整名称不一致。']
                    if atom == value and not value.isascii() and (
                            (left and focus[left - 1].isalnum()) or
                            (right < len(focus) and focus[right].isalnum())):
                        return selection, '', ['无法确定原文中范围名称的边界。']
                    fragments.append((left, right))
            for left, right in fragments:
                masked[left:right] = ['\n'] * (right - left)
        selection = {**(selection or {}), key: v}
    # Older parser responses may omit a structured scope. Still verify explicit
    # project references and keep unsupported exclusions unresolved.
    # A removed span is a boundary. Two operands on opposite sides cannot
    # become a new value + dimension simply because whitespace separates them.
    return _resolve_scope(retriever, scopes, selection, re.split(r'\n+', ''.join(masked)), excluded, is_path=is_path)


def _unexplained_spans(text, classified):
    expressions = [re.escape(s) for s in sorted(set(classified), key=len, reverse=True) if s]
    masked = list(text)
    regions = []
    if expressions:
        for match in re.finditer('|'.join(expressions), text, flags=re.I):
            # Consecutive annotated mentions can cover an entire undelimited
            # identifier. A lone annotation inside an unrelated word cannot.
            if regions and match.start() == regions[-1][1]:
                regions[-1] = (regions[-1][0], match.end())
            else:
                regions.append(match.span())
    for start, end in regions:
        if is_literal_span(text, start, end):
            masked[start:end] = ['\n'] * (end - start)
    masked = ''.join(masked)
    return list(dict.fromkeys(part.strip() for part in re.split(r'[。；;，,！？!?\n]+', masked)
                              if any(c.isalnum() or c in '<>≤≥=' for c in part)))


def _contains_name(text, name):
    return any(is_literal_span(text, *m.span()) for m in re.finditer(re.escape(name), text, re.I))


def _ground_target(phrases, kinds):
    """Return the unique visible output type within the parser's output wording."""
    found = set()
    for phrase in phrases:
        hits = {k for k in kinds if k and _contains_name(phrase, k)}
        found.update(k for k in hits if not any(k != other and k in other for other in hits))
    return found.pop() if len(found) == 1 else None


def _finish_traversal(plan, path=None):
    """Carry only unambiguous, grounded endpoints into path search."""
    literal = _offline_path_endpoints(plan['focus'], plan['conditions'])
    if path and any(path.get(k) and literal.get(k) and path[k] != literal[k] for k in ('start', 'end')):
        plan['unresolved'].append('路径起点或终点与问句中的原文顺序不一致。')
    path = {**literal, **{k: v for k, v in (path or {}).items() if v}}
    plan['operation'] = 'traverse'
    ids = {s['text']: s['candidates'][0]['node_id'] for s in plan['conditions'] if len(s['candidates']) == 1}
    if not ids or plan['candidate_limit'] or len(ids) != len(plan['conditions']):
        plan['unresolved'].append('需要确认关联路径上的对象。')
    for role in ('start', 'end', 'via'):
        terms = path.get(role) or []
        if isinstance(terms, str):
            terms = [terms]
        if not terms:
            continue
        if any(term not in ids for term in terms):
            plan['unresolved'].append('无法确认路径中指定的起点、终点或经过的对象。')
        elif role == 'via':
            plan['via_ids'] = [ids[t] for t in terms]
        else:
            plan['start_id' if role == 'start' else 'target_id'] = ids[terms[0]]
    free = _unexplained_spans(plan['focus'], [s['text'] for s in plan['conditions']])
    directions = {key for key, pattern in _EDGE_DIRECTIONS.items() if any(pattern.search(span) for span in free)}
    # A model can quote a direction expressed in another language. Inspect
    # the literal evidence for contradictory instructions where recognized.
    if path.get('direction'):
        quote = path.get('direction_text')
        if quote and quote in plan['focus'] and (directions or _residuals(quote, [], None, path=True)):
            directions.add(path['direction'])
        elif path['direction'] != 'both':
            plan['unresolved'].append('未在当前问句中核实路径方向。')
    if len(directions) > 1:
        plan['unresolved'].append('路径方向存在多种解释。')
    elif directions:
        plan['direction'] = directions.pop()
    plan['intent'] = 'clarify' if plan['unresolved'] else 'traverse'
    return plan


def _compile_interpretation(retriever, scopes, requested, query, parsed, *, force_joint, force_traverse, drill_role,
                            include_endpoint_variants=False):
    selection, focus, issues = _model_scope(retriever, scopes, requested, query, parsed,
                                           is_path=force_traverse or parsed.get('intent') == 'traverse')
    plan = _new_plan(selection, focus, 'model')
    if issues:
        plan.update(intent='clarify', scope_unresolved=True, unresolved=issues, focus='')
        return plan
    cohorts = retriever.authorized_cohorts(scopes, selection)
    kinds = retriever.index.authorized_kinds(cohorts)
    clauses = parsed.get('constraints', ())
    proposed = [text for text in parsed['mentions'] if text in focus]
    target = parsed.get('target')
    intent = parsed.get('intent') or 'search'
    intent = {'hierarchy': 'drilldown', 'cooccurrence': 'dimensional'}.get(intent, intent)
    if force_traverse:
        intent = 'traverse'
    if force_joint:
        intent = 'dimensional'
    elif target and intent in {'search', 'explore'}:
        # A requested endpoint category does not imply a taxonomy. Only
        # explicit hierarchy intent should demand parent/child edges.
        intent = 'traverse'
    if intent == 'grouping':
        # Preserve the full remainder for the normal constraint/coverage checks.
        # Supplying a grouping target does not establish a scope or a value.
        if target and len(proposed) == 1:
            plan['grouping_spans'] = (proposed[0], target, list(clauses))
            plan['grouping_names'] = next((p['names'] for p in parsed.get('equivalents', ())
                                          if p['text'] == target), [])
        intent = 'search'
    path = parsed.get('traversal') or {}
    traverse = intent == 'traverse'
    target_phrases = [target] if target else parsed.get('framing', ()) if intent == 'drilldown' else ()
    plan['requested_level'] = _ground_target(target_phrases, kinds)
    if target and traverse and not plan['requested_level'] and _resolve_text(retriever.index, target, cohorts)[0]:
        proposed.append(target)
        path = {**path, 'end': path.get('end') or target}
    elif target and not plan['requested_level']:
        plan['unresolved'].append('当前可见图谱中未确认目标类型“' + target + '”。')
        plan['unbound_mentions'].append({'text': target, 'reason': 'unverified_type'})

    # Models can put a categorical operand in a filter, or combine a name
    # with its qualifier. Recover the actual operands, leaving every other
    # part of the filter to be checked below.
    known = retriever.index.terms_in_text(focus, cohorts)
    exact_terms = [s for s in decompose_query(focus, known_terms=known) if s.source == 'exact_term']
    if traverse:
        # An explicit, exact endpoint can be recovered even if the model
        # mistakenly put that occurrence in scopes instead of mentions.
        for term in exact_terms:
            if any(_endpoint_role(focus[:m.start()]) for m in re.finditer(re.escape(term.surface), focus, re.I)):
                if term.surface not in proposed:
                    proposed.append(term.surface)
    literal = [s.surface for s in exact_terms
               if any(s.normalized in normalize_term(m) or normalize_term(m) in s.normalized for m in proposed)
               or any(s.normalized in normalize_term(clause) for clause in clauses)]
    surfaces = _combine_mentions(focus, literal, proposed)
    if len(surfaces) > 12:
        plan['candidate_limit'] = True
        plan['unresolved'].append('条件数量超出本次联合查询范围。')
    slots = plan['conditions']
    pending = _bind_exact(retriever, cohorts, plan, surfaces[:12])
    # Do not silently drop the middle of a coarse model phrase when two
    # independently named operands account for only part of it.
    for span in proposed:
        contained = [s for s in surfaces if normalize_term(s) in normalize_term(span)]
        if len(contained) > 1 and span not in contained:
            pending.extend(_unexplained_spans(span, contained))
    equivalents = {item['text']: item['names'] for item in parsed.get('equivalents', ()) if item['text'] in proposed}
    # First try the entire compound. Only split a phrase if neither its
    # complete name nor its independently corroborated alias resolves.
    compounds = {s: [part for part in literal if part != s and _contains_name(s, part)]
                 for s in pending}
    compounds = {s: parts for s, parts in compounds.items() if parts}
    if compounds:
        links = link_mentions(retriever, list(compounds), cohorts, paraphrases=equivalents)
        for link in links:
            phrase = link['text']
            pending.remove(phrase)
            if link['status'] == 'linked':
                best = link['candidates'][0]
                slots.append({'text': phrase, 'role': best['kind'], 'match': 'semantic', 'candidates': [best]})
            else:
                parts = compounds[phrase]
                room = max(0, 12 - len(slots))
                plan['candidate_limit'] |= len(parts) > room
                pending.extend(_bind_exact(retriever, cohorts, plan, parts[:room]))
                pending.extend(_unexplained_spans(phrase, parts))
    _link_open_terms(retriever, cohorts, plan, pending, equivalents)
    slots.sort(key=lambda s: normalize_term(focus).find(normalize_term(s['text'])))

    def unverified(text):
        if text not in plan['unverified_predicates']:
            plan['unverified_predicates'].append(text)
        if not any(i['text'] == text for i in plan['unbound_mentions']):
            plan['unbound_mentions'].append({'text': text, 'reason': 'unverified_condition'})
            plan['unresolved'].append('尚无法验证限定条件：' + text)

    relation = path.get('relation') if traverse else None
    if relation:
        # A parser may copy the verb in "X [connects to] Y" into the
        # relation-type field. Names inside a relation filter stay requirements.
        anchors = [s['text'] for s in slots]
        generic_link = (focus.count(relation) == 1 and not _residuals(relation, [], None, path=True) and
                        any(re.search(re.escape(a) + r'\s*' + re.escape(relation) + r'\s*(?:到|至)?\s*'
                                      + re.escape(b), focus, re.I) for a, b in zip(anchors, anchors[1:])))
        if not generic_link:
            visible_relations = retriever.index.authorized_relations(cohorts)
            matches = [r for r in visible_relations if normalize_term(r) == normalize_term(relation)]
            if include_endpoint_variants and not matches and relation in focus:
                expanded = retriever.index.expand_endpoint_relation_types([relation], cohorts)
                if set(visible_relations).intersection(expanded):
                    matches = [relation]
            if relation in focus and len(matches) == 1:
                plan['relation_types'] = matches
                # A bare subject-predicate-object question asks about that
                # particular edge. A potentially reversed, multihop walk
                # cannot verify the asserted predicate.
                bare_predicate = any(re.search(re.escape(a) + r'\s*' + re.escape(relation)
                                               + r'(?:了)?\s*' + re.escape(b), focus, re.I)
                                     for a, b in zip(anchors, anchors[1:]))
                if bare_predicate and not _PATH_OPERATION.search(focus):
                    unverified(relation)
            else:
                unverified(relation)
    operands = sorted({s['text'] for s in slots}, key=len, reverse=True)
    if plan.get('relation_types'):
        operands.append(relation)
    for clause in clauses:
        active = _mask_scopes(clause, _scope_matches(clause, (selection or {}).items()))
        remainder = _unexplained_spans(active, operands)
        if any(_residuals(part, [], None, path=traverse) for part in remainder):
            unverified(clause)

    # Inspect the *unexplained spans* for omitted restrictions. They are
    # language to interpret, never extra entities to substitute into the graph.
    classified = [*proposed, *clauses]
    if relation:
        classified.append(relation)
    if plan['requested_level']:
        classified.append(plan['requested_level'])
    elif target:
        classified.append(target)
    gaps = _unexplained_spans(focus, classified)
    # Skip a second interpretation of the syntax of an operation the
    # planner has already grounded. Check whole gaps, never edit inside a
    # proposed name. An unexecuted predicate always needs separate evidence.
    output_wording = [s for s in target_phrases if plan['requested_level'] and
                      _contains_name(s, plan['requested_level'])]
    operands = tuple(s for s in classified if s)
    def between_operands(gap):
        return all(focus[:m.start()].rstrip().endswith(operands) and focus[m.end():].lstrip().startswith(operands)
                   for m in re.finditer(re.escape(gap), focus))

    gaps = [gap for gap in gaps if _PREDICATE.search(gap) or _NEG.search(gap)
            or _residuals(gap, [], None, path=traverse)
            or not (traverse or between_operands(gap) or any(gap in phrase for phrase in output_wording))]
    reviewer = getattr(retriever.mention_extractor, 'review_coverage', None)
    omit = reviewer(query, gaps) if reviewer and gaps else None
    for gap in gaps:
        if (any(c.isdigit() or c in '<>≤≥=' for c in gap)
                or _PREDICATE.search(gap) or _NEG.search(gap)):
            unverified(gap)
        elif omit is not None:
            if gap not in omit:
                unverified(gap)
        elif _residuals(gap, [], None, path=traverse):
            unverified(gap)

    if traverse:
        return _finish_traversal(plan, path)
    roles = {s['role'] for s in slots if s['role']}
    if intent == 'overview':
        if slots or proposed or parsed.get('constraints'):
            plan['unresolved'].append('图谱统计需要与问题中的筛选条件使用同一范围。')
        plan['overview_metric'] = parsed.get('overview_metric') or 'community'
    elif force_joint or len(slots) >= 2 and intent in {'drilldown', 'dimensional'}:
        intent = 'dimensional'
    if intent == 'drilldown' or intent == 'dimensional' and (target or plan['requested_level'] or drill_role):
        parents = _parent_roles(retriever.index, cohorts, slots, plan['requested_level']) if plan['requested_level'] else set()
        if drill_role:
            parents = {drill_role} & roles
        if not parents and len(roles) == 1:
            parents = roles
        if len(parents) == 1:
            plan['drill_role'] = next(iter(parents))
        elif slots:
            plan['unresolved'].append('未确认要展开哪一类对象的子级。')
        if not force_joint and len(slots) == 1:
            intent = 'drilldown'
    if intent == 'dimensional' and len(slots) < 2 and not plan['unresolved']:
        plan['unresolved'].append('未找到足够的条件来验证共同事实。')
    if not slots and intent != 'overview' and not plan['unresolved']:
        unverified(focus or query)
    plan['point_only'] = intent == 'point'
    if intent == 'point':
        intent = 'search'
    plan['intent'] = 'clarify' if plan['unresolved'] else intent
    return plan


def _offline_path_endpoints(focus, slots):
    """Read explicit from/via/to labels; mention order alone does not set direction."""
    path = {'via': []}
    for slot in slots:
        text = slot['text']
        for match in re.finditer(re.escape(text), focus, re.I):
            role = _endpoint_role(focus[:match.start()])
            if role == 'via':
                path['via'].append(text)
            elif role:
                path[role] = text
    return path


def build_plan(retriever, scopes, query, business_context, conditions=(), drill_role=None, force_joint=False,
               force_traverse=False, include_endpoint_variants=False):
    """Roles come from the visible type of each grounded candidate, never from word order."""
    if query.strip() and not conditions and not force_joint and not force_traverse:
        selection = normalize_context_selection(business_context)
        cohorts = retriever.authorized_cohorts(scopes, selection)
        hits = retriever.index.exact_names([query], scopes=cohorts, limit_per_term=6, include_codes=True)
        if hits.get(normalize_term(query)):
            _, remainder, issues = _resolve_scope(retriever, scopes, selection, [query], ())
            # An exact object name may itself be the whole value + dimension.
            # A composite question with additional scoped terms still needs
            # the regular planner, even if someone stored it as a label.
            if not issues and (not remainder.strip() or normalize_term(remainder) == normalize_term(query)):
                plan = _new_plan(selection, query, 'exact_name')
                _bind_exact(retriever, cohorts, plan, [query])
                return plan
    model_attempted = False
    if not conditions and retriever.mention_extractor and retriever.authorized_cohorts(scopes, business_context):
        model_attempted = True
        parsed = retriever.mention_extractor(query)
        if parsed is not None:
            return _compile_interpretation(retriever, scopes, business_context, query, parsed,
                                           force_joint=force_joint, force_traverse=force_traverse, drill_role=drill_role,
                                           include_endpoint_variants=include_endpoint_variants)
    clauses, excluded = _clauses(query)
    selection, focus, unresolved = _resolve_scope(retriever, scopes, business_context, clauses, excluded,
                                                 is_path=force_traverse)
    cohorts = retriever.authorized_cohorts(scopes, selection)
    plan = _new_plan(selection, focus, 'structured' if conditions else 'index')
    if not conditions:
        if model_attempted:
            plan['parser_fallback'] = 'model_interpretation_unavailable'
        elif retriever.mention_extractor is None:
            plan['parser_fallback'] = 'model_parser_not_configured'
        else:
            plan['parser_fallback'] = 'no_visible_context'
    plan.update(drill_role=drill_role, unresolved=unresolved, point_only=bool(_POINT_ONLY.search(focus)))
    if unresolved:
        plan.update(intent='clarify', scope_unresolved=True, focus='')
        return plan
    structured = bool(conditions)
    kinds = retriever.index.authorized_kinds(cohorts)
    plan['requested_level'] = _target_kind(focus, kinds) if not structured else None
    path_requested = force_traverse or not force_joint and not structured and bool(_PATH_OPERATION.search(focus))
    if not structured and focus.strip():
        _ground_index_query(retriever, cohorts, focus, retriever.index.terms_in_text(focus, cohorts), plan, path=path_requested)
    level = _LEVEL.search(focus) if not structured else None
    if level and not plan['requested_level'] and level.group(1) not in {'底', '最底层', '最后一级', '叶子节点', '叶节点'}:
        unresolved.append('目标层级尚未对应到当前图谱的节点类型。')
    for item in conditions[:12]:
        kind = item.get('kind')
        if item.get('node_id'):
            nodes = retriever.index.load_authorized_nodes([item['node_id']], scopes=cohorts)
            candidates = [{'node_id': n['id'], 'kind': n['kind']} for n in nodes.values() if not kind or n['kind'] == kind]
        else:
            candidates, capped = _resolve_text(retriever.index, item['text'], cohorts, kind)
            plan['candidate_limit'] |= capped
        kinds = {c['kind'] for c in candidates}
        role = item.get('role') or kind or (next(iter(kinds)) if len(kinds) == 1 else None)
        plan['conditions'].append({'text': item.get('text') or item.get('role') or '节点', 'role': role,
                                   'match': 'node_id' if item.get('node_id') else 'exact', 'candidates': candidates})
    if len(conditions) > 12:
        plan['candidate_limit'] = True
        unresolved.append('条件数量超出本次联合查询范围。')
    slots = plan['conditions']
    roles = {s['role'] for s in slots if s['role']}
    associated_type = (plan['requested_level'] and slots and not _ACTION.search(focus) and not unresolved
                       and not _parent_roles(retriever.index, cohorts, slots, plan['requested_level']))
    if ((path_requested and (force_traverse or any(_PATH_OPERATION.search(part) for part in
            _unexplained_spans(focus, [s['text'] for s in slots]))))
            or associated_type and not force_joint and not structured):
        residual = _residuals(focus, slots, plan['requested_level'], path=True)
        if residual:
            unresolved.append('关联路径还有未确认的条件：' + '、'.join(residual))
        return _finish_traversal(plan)
    if _OVERVIEW.search(focus) and not force_joint and not structured and not _ACTION.search(focus):
        if unresolved or slots or re.search('相关|有关|限定|过滤|只统计|仅统计|大于|小于|超过|只针对|只考虑', focus):
            plan['intent'] = 'clarify'
            unresolved.append('图谱统计以发布范围为单位，所提条件需要单独核实。')
        else:
            plan['intent'] = 'overview'
            plan['overview_metric'] = 'community'
            if not re.search(r'社[区群]|概览|概况', focus):
                for term, metric in [('入度', 'in_degree'), ('出度', 'out_degree'), ('度中心度', 'degree_centrality'),
                                     ('中心度', 'pagerank'), ('核心节点', 'pagerank'), ('关键节点', 'pagerank'),
                                     ('枢纽', 'pagerank'), ('pagerank', 'pagerank')]:
                    if term in focus.casefold():
                        plan['overview_metric'] = metric
                        break
        return plan
    drill = bool(_ACTION.search(focus) or drill_role or plan['requested_level'])
    # Extract the requested dimension from graph types or an explicit entity
    # mention near the operation. Preserve suffixes such as "层级" / "类别".
    if drill and not drill_role:
        actions = [part for part in re.split('[，。]', focus) if _ACTION.search(part)]
        names = set()
        for part in actions:
            for role in roles:
                if re.search(r'(?:按|沿|看|到|从|依照|根据|展开)\s*' + re.escape(role), part, re.I):
                    names.add(role)
            after = _ACTION.split(part)[-1]
            names.update(s['role'] for s in slots if s['role'] and s['text'] in after)
        if not names and plan['requested_level']:
            names.update(_parent_roles(retriever.index, cohorts, slots, plan['requested_level']))
        if len(names) == 1:
            plan['drill_role'] = names.pop()
        elif len(roles) == 1:
            plan['drill_role'] = next(iter(roles))
        elif len(slots) > 1:
            unresolved.append('请说明要查看哪个已匹配对象的子级。')
    if drill_role and drill_role not in roles:
        unresolved.append('要展开的对象类型与已确认的条件不符。')
    # Compact noun phrases can express intersecting qualifiers. A list
    # of entities of different kinds by itself only asks for those objects.
    joint_wording = re.search('同时满足|交集|同时出现|共同记录', focus)
    adjacent = any(re.search(re.escape(a['text']) + '的?' + re.escape(b['text']), focus, re.I)
                   for a, b in zip(slots, slots[1:]))
    if force_joint or len(slots) >= 2 and (drill or joint_wording or len(roles) > 1 and adjacent):
        plan['intent'] = 'dimensional'
    elif drill:
        plan['intent'] = 'drilldown'
    elif _EXPLORE.search(focus):
        plan['intent'] = 'explore'
    if re.search('或者|还是|二选一|对比|相比|比较', focus) and plan['intent'] in {'dimensional','drilldown'}:
        unresolved.append('问题包含比较或备选条件，需要分别确认查询范围。')
    if any(len({c['kind'] for c in s['candidates']}) > 1 for s in slots) and plan['intent'] in {'dimensional','drilldown'}:
        unresolved.append('同名对象属于不同类型，请明确所指类型。')
    if drill and not slots:
        unresolved.append('未找到可确认的层级展开起点。')
    if (not structured and plan['intent'] == 'dimensional' and len(roles) < len(slots)
            and all(s['role'] for s in slots) and not re.search('同时|共同|一起', focus)):
        unresolved.append('同一维度出现多个取值，请说明要分别查询还是求交集。')
    if focus.strip() and (plan['intent'] in {'drilldown', 'dimensional'} or _RESTRICT.search(focus) or _PREDICATE.search(focus)):
        by_id = [c['node_id'] for slot in slots if slot['match'] == 'node_id' for c in slot['candidates']]
        if by_id:
            labels = retriever.index.authorized_labels(by_id, scopes=cohorts)
            # A name stated alongside a selected ID must be witnessed by the
            # actual observation used in the joint fact, even for a shared ID.
            for slot in slots:
                if slot['match'] == 'node_id' and slot['candidates']:
                    label = labels.get(slot['candidates'][0]['node_id'])
                    if label and _contains_name(focus, label):
                        slot.update(text=label, match='exact')
        residual = _residuals(focus, slots, plan['requested_level'], framing=by_id)
        if residual:
            if not plan['suggestions']:
                plan['suggestions'] = _lexical_suggestions(retriever, scopes, selection, residual)
            remaining = [part for part in residual if not any(part in issue for issue in unresolved)]
            if remaining:
                unresolved.append('有条件尚未对应到可确认的图谱对象：' + '、'.join(remaining[:5]))
    if unresolved:
        plan['intent'] = 'clarify'
    return plan
