"""Ground a question in the active, authorized graph before choosing a traversal."""
from __future__ import annotations

import re

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
_NEG = re.compile(r'不要(?:再)?(?:看|分析|查)?|不看|排除|忽略|不考虑|不包括|不包含|不含|不关注|无需考虑|不关心|除外')
_HISTORY = re.compile(r'^(?:(?:背景|例如|举例)\s*[:：]|(?:上次|此前|之前)(?:讨论|查询|看过))')
_FOCUS = re.compile(r'(?:本次|这次|现在)(?:请|要|只)')
_PROJECT_NAMES = {'project', 'project_id', 'project_name', '项目', '项目名', '项目名称'}
_RESTRICT = re.compile(r'限定|过滤|要求|约束|须为|必须|条件|只要|只统计|只看|仅看|'
                       r'大于|小于|超过|至多|至少|低于|高于|不超过|不少于|[<>≥≤=]|且|尚未|已经')
_SCOPE_HINT = re.compile(
    r'[a-z\d_-]+\s*项目|[\w-]+\s*项目(?=的|中|里|\s|[，,。]|$)|项目\s*(?:[=:：]\s*\S|[a-z\d_-]+)|'
    r'\bproject\s*[:=]\s*\S|\bproject\s+[a-z\d_-]+', re.I)


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


def _scope_matches(text, authorized):
    for name, value in authorized:
        escaped = re.escape(str(value))
        key = re.escape(name)
        if name.casefold() in _PROJECT_NAMES:
            key = '(?:project(?:_id|_name)?|项目(?:名称|名)?)'
        # Include a boundary for Latin identifiers so "M" cannot select "AM".
        pattern = rf'(?P<value_first>(?<![a-zA-Z0-9_]){escaped}\s*{key}(?![a-zA-Z0-9_]))|{key}\s*[=:：]?\s*{escaped}(?![a-zA-Z0-9_])'
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


def _resolve_scope(retriever, scopes, requested, clauses, excluded):
    selection = normalize_context_selection(requested)
    dimensions = set()
    for token in retriever.authorized_cohorts(scopes, None):
        dimensions.update(retriever.cohort_contexts[token]['dimensions'].items())
    text = '，'.join(clauses)
    unresolved = []
    matches = list(_scope_matches(text, dimensions))
    for name, value, _ in matches:
        selection = dict(selection or {})
        if name in selection and selection[name] != value:
            unresolved.append('出现互不一致的业务范围。')
        selection[name] = value
    text = _mask_scopes(text, matches)
    if _SCOPE_HINT.search(text):
        unresolved.append('所提项目无法在当前可见范围内确认。')
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
    r'拆开|细分|展开|分组|数据|结果|内容'
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


def _residuals(text, slots, level, framing=()):
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


def _ground_index_query(retriever, cohorts, focus, terms, plan):
    """Limited fallback for installations without a working question parser."""
    literal = [s.surface for s in decompose_query(focus, known_terms=terms) if s.source == 'exact_term']
    if len(literal) > 12:
        plan['candidate_limit'] = True
        plan['unresolved'].append('条件数量超出本次联合查询范围。')
    _bind_exact(retriever, cohorts, plan, literal[:12])
    residual = _residuals(_OVERVIEW.sub(' ', focus), plan['conditions'], plan['requested_level'])
    eligible = [s for s in residual if len(s) > 1 and not _PREDICATE.search(s)
                and not _RESTRICT.search(s) and not _NEG.search(s) and not s.isnumeric()]
    _link_open_terms(retriever, cohorts, plan, eligible)


def _new_plan(selection, focus, parser):
    return {'intent': 'search', 'business_context': selection, 'conditions': [], 'drill_role': None,
            'unresolved': [], 'unbound_mentions': [], 'unverified_predicates': [], 'suggestions': [], 'point_only': False,
            'requested_level': None, 'excluded_terms': [], 'focus': focus.strip(), 'candidate_limit': False,
            'parser': parser}


def _model_scope(retriever, scopes, requested, query, parsed):
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
    for scope in parsed.get('scopes', ()):
        phrase = scope['text']
        dimension, value = scope['dimension'], scope['value']
        if not phrase or not dimension or not value or dimension not in phrase or value not in phrase:
            return selection, '', ['范围名称、维度与原文不一致。']
        keys = [(key, v) for key, v in available if normalize_term(v) == normalize_term(value) and
                (normalize_term(key) == normalize_term(dimension) or
                 normalize_term(key) in _PROJECT_NAMES and normalize_term(dimension) in _PROJECT_NAMES)]
        if len(keys) != 1:
            return selection, '', ['所提范围无法与当前可见的业务范围唯一对应。']
        key, v = keys[0]
        if key in (selection or {}) and selection[key] != v:
            return selection, '', ['出现互不一致的业务范围。']
        # Align against active text, not just the quoted fragment. "京项目" is
        # well formed in isolation even when copied from inside "南京项目".
        phrases = list(re.finditer(re.escape(phrase), focus))
        if not phrases:
            return selection, '', ['所提范围只出现在背景、排除条件中或未在原文找到。']
        adjacent = list(_scope_matches(phrase, keys))
        full = [hit for _, _, hit in _scope_matches(focus, keys)]
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
    return _resolve_scope(retriever, scopes, selection, re.split(r'\n+', ''.join(masked)), excluded)


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


def _compile_interpretation(retriever, scopes, requested, query, parsed, *, force_joint, drill_role):
    selection, focus, issues = _model_scope(retriever, scopes, requested, query, parsed)
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
    target_phrases = [target] if target else parsed.get('framing', ()) if intent == 'drilldown' else ()
    plan['requested_level'] = _ground_target(target_phrases, kinds)
    if target and not plan['requested_level']:
        plan['unresolved'].append('当前可见图谱中未确认目标类型“' + target + '”。')
        plan['unbound_mentions'].append({'text': target, 'reason': 'unverified_type'})

    # Models can put a categorical operand in a filter, or combine a name
    # with its qualifier. Recover the actual operands, leaving every other
    # part of the filter to be checked below.
    known = retriever.index.authorized_terms(cohorts)
    literal = [s.surface for s in decompose_query(focus, known_terms=known) if s.source == 'exact_term'
               and (any(s.normalized in normalize_term(m) or normalize_term(m) in s.normalized for m in proposed)
                    or any(s.normalized in normalize_term(clause) for clause in clauses))]
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

    operands = sorted({s['text'] for s in slots}, key=len, reverse=True)
    for clause in clauses:
        active = _mask_scopes(clause, _scope_matches(clause, (selection or {}).items()))
        remainder = _unexplained_spans(active, operands)
        if any(_residuals(part, [], None) for part in remainder):
            unverified(clause)

    # Inspect the *unexplained spans* for omitted restrictions. They are
    # language to interpret, never extra entities to substitute into the graph.
    classified = [*proposed, *clauses]
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
            or _residuals(gap, [], None)
            or not (between_operands(gap) or any(gap in phrase for phrase in output_wording))]
    reviewer = getattr(retriever.mention_extractor, 'review_coverage', None)
    omit = reviewer(query, gaps) if reviewer and gaps else None
    for gap in gaps:
        if (any(c.isdigit() or c in '<>≤≥=' for c in gap)
                or _PREDICATE.search(gap) or _NEG.search(gap)):
            unverified(gap)
        elif omit is not None:
            if gap not in omit:
                unverified(gap)
        elif _residuals(gap, [], None):
            unverified(gap)

    roles = {s['role'] for s in slots if s['role']}
    if intent == 'overview':
        if slots or proposed or parsed.get('constraints'):
            plan['unresolved'].append('图谱统计需要与问题中的筛选条件使用同一范围。')
        plan['overview_metric'] = parsed.get('overview_metric') or 'community'
    elif force_joint or len(slots) >= 2 and (intent in {'drilldown', 'dimensional'} or len(roles) > 1):
        intent = 'dimensional'
    if target or plan['requested_level'] or intent == 'drilldown' or drill_role:
        parents = _parent_roles(retriever.index, cohorts, slots, plan['requested_level']) if plan['requested_level'] else set()
        if drill_role:
            parents = {drill_role} & roles
        if not parents and len(roles) == 1:
            parents = roles
        if len(parents) == 1:
            plan['drill_role'] = next(iter(parents))
        elif slots:
            plan['unresolved'].append('未确认应沿哪个维度下钻。')
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


def build_plan(retriever, scopes, query, business_context, conditions=(), drill_role=None, force_joint=False):
    """Roles come from the visible type of each grounded candidate, never from word order."""
    if query.strip() and not conditions and not force_joint:
        selection, text, issues = _resolve_scope(retriever, scopes, business_context, [query], ())
        if not issues and normalize_term(text) == normalize_term(query):
            cohorts = retriever.authorized_cohorts(scopes, selection)
            names = retriever.index.exact_names([query], scopes=cohorts, limit_per_term=6)
            hits = names.get(normalize_term(query), [])
            if hits:
                plan = _new_plan(selection, query, 'exact_name')
                _bind_exact(retriever, cohorts, plan, [query])
                return plan
    if not conditions and retriever.mention_extractor and retriever.authorized_cohorts(scopes, business_context):
        parsed = retriever.mention_extractor(query)
        if parsed is not None:
            return _compile_interpretation(retriever, scopes, business_context, query, parsed,
                                           force_joint=force_joint, drill_role=drill_role)
    clauses, excluded = _clauses(query)
    selection, focus, unresolved = _resolve_scope(retriever, scopes, business_context, clauses, excluded)
    cohorts = retriever.authorized_cohorts(scopes, selection)
    plan = _new_plan(selection, focus, 'structured' if conditions else 'index')
    plan.update(drill_role=drill_role, unresolved=unresolved, point_only=bool(_POINT_ONLY.search(focus)))
    if unresolved:
        plan.update(intent='clarify', scope_unresolved=True, focus='')
        return plan
    terms = retriever.index.authorized_terms(cohorts)
    structured = bool(conditions)
    kinds = retriever.index.authorized_kinds(cohorts)
    plan['requested_level'] = _target_kind(focus, kinds) if not structured else None
    if not structured and focus.strip():
        _ground_index_query(retriever, cohorts, focus, terms, plan)
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
                                   'candidates': candidates})
    if len(conditions) > 12:
        plan['candidate_limit'] = True
        unresolved.append('条件数量超出本次联合查询范围。')
    slots = plan['conditions']
    roles = {s['role'] for s in slots if s['role']}
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
            unresolved.append('请说明沿哪个已匹配的维度向下查看。')
    if drill_role and drill_role not in roles:
        unresolved.append('下钻维度没有对应到已确认的条件。')
    if force_joint or len(slots) >= 2 and (len(roles) > 1 or drill or re.search('同时满足|交集|同时出现', focus)):
        plan['intent'] = 'dimensional'
    elif drill:
        plan['intent'] = 'drilldown'
    elif _EXPLORE.search(focus):
        plan['intent'] = 'explore'
    if re.search('或者|还是|二选一|对比|相比|比较', focus) and plan['intent'] in {'dimensional','drilldown'}:
        unresolved.append('问题包含比较或备选条件，需要分别确认分析范围。')
    if any(len({c['kind'] for c in s['candidates']}) > 1 for s in slots) and plan['intent'] in {'dimensional','drilldown'}:
        unresolved.append('同名对象属于不同类型，请明确所指类型。')
    if drill and not slots:
        unresolved.append('未找到可确认的下钻起点。')
    if (not structured and plan['intent'] == 'dimensional' and len(roles) < len(slots)
            and all(s['role'] for s in slots) and not re.search('同时|共同|一起', focus)):
        unresolved.append('同一维度出现多个取值，请说明要分别查询还是求交集。')
    if not structured and (plan['intent'] in {'drilldown', 'dimensional'}
                           or _RESTRICT.search(focus) or _PREDICATE.search(focus)):
        residual = _residuals(focus, slots, plan['requested_level'])
        if residual:
            if not plan['suggestions']:
                plan['suggestions'] = _lexical_suggestions(retriever, scopes, selection, residual)
            remaining = [part for part in residual if not any(part in issue for issue in unresolved)]
            if remaining:
                unresolved.append('有条件尚未对应到可确认的图谱对象：' + '、'.join(remaining[:5]))
    if unresolved:
        plan['intent'] = 'clarify'
    return plan
