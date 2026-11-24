"""Local SQLite relational graph store with scope-bound terms and bounded BFS expansion."""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from itertools import chain, groupby
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Sequence
import unicodedata
from lightgraph.construction.fusion import definition_contract
from lightgraph.contracts import canonical, digest
from lightgraph.errors import LightGraphError
from .hierarchy_bindings import mapped_direction, corroborated_level_keys

# LIMIT caps returned routes, but sorting a dense two-hop join can take
# quadratic work before LIMIT is applied. Bound SQLite VM work as well.
_ROUTE_SQL_STEPS = 40_000_000

_NAME_ROLES = ('chinese_name', 'english_name', 'display_name', 'alias')
_TERM_ROLES = {
    'name': _NAME_ROLES,
    'description': ('description',),
    'all': (*_NAME_ROLES, 'identity_code', 'description'),
}


def normalize_term(value: Any) -> str:
    return ' '.join(unicodedata.normalize('NFKC', str(value or '')).casefold().split())


def _endpoint_specializations(definitions):
    """Only a named primary and its verified endpoint partitions share this filter."""
    by_name = defaultdict(list)
    for definition in definitions:
        if definition.get('type'):
            by_name[definition['type']].append(definition)
    projected = {}
    for name, declarations in by_name.items():
        if len(declarations) != 1:
            continue
        definition, = declarations
        family = definition.get('original_type')
        marker = definition.get('definition_variant')
        if not family or not isinstance(marker, dict) or marker.get('version') != 1:
            continue
        parent = marker.get('endpoint_projection')
        if (not isinstance(parent, str) or not re.fullmatch('[0-9a-f]{64}', parent)
                or not definition.get('source_type') or not definition.get('target_type')):
            continue
        contract = definition_contract(definition)
        if marker.get('contract_digest') != digest(contract):
            continue
        for field in ('source_type', 'target_type', 'endpoint_projection'):
            contract.pop(field, None)
        description = ' '.join((definition.get('description') or '').split())
        projected[name] = (family, parent, digest(contract, description))
    for name, signature in projected.items():
        family = signature[0]
        if name != family and projected.get(family) == signature:
            yield family, name


def observed_text_properties(observation):
    """Yield source property names and textual values from one authorized assertion."""
    properties = observation.get('properties') or {}
    original = observation.get('property_observations') or []
    mapped = {p.get('property') for p in original if 'raw_value' in p and p.get('original_property')}
    values = chain(((p['original_property'], p['raw_value']) for p in original
                    if 'raw_value' in p and p.get('original_property')),
                   ((key, value) for key, value in properties.items() if key not in mapped))
    for name, value in values:
        stack = [value]
        while stack:
            item = stack.pop()
            if isinstance(item, str) and item.strip():
                yield name, item
            elif isinstance(item, list):
                stack.extend(reversed(item))
            elif isinstance(item, dict):
                stack.extend(reversed(item.values()))


def _source_excerpt(raw, query, norm):
    # SQLite matched NFKC/casefold text with collapsed whitespace. Map that
    # location back into the original string before taking a verbatim excerpt.
    start = raw.find(query)
    if start < 0:
        offset = normalize_term(raw).find(norm)
        low, high = 0, len(raw)
        while low < high:
            middle = (low + high) // 2
            if len(normalize_term(raw[:middle])) <= offset:
                low = middle + 1
            else:
                high = middle
        start = max(0, low - 1)
    start = max(0, start - 90)
    return raw[start:start + 240]


class SQLiteGraphIndex:
    """Local relational topology & lexical index enforcing scope visibility on every read."""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS kg_nodes (
                    node_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    identity_json TEXT NOT NULL,
                    identity_types_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS kg_node_scope_views (
                    node_id TEXT NOT NULL,
                    scope_ref TEXT NOT NULL,
                    chinese_name TEXT,
                    english_name TEXT,
                    display_name TEXT NOT NULL,
                    aliases_json TEXT NOT NULL,
                    description TEXT,
                    source_tables_json TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    observations_json TEXT NOT NULL,
                    PRIMARY KEY (node_id, scope_ref)
                );
                CREATE TABLE IF NOT EXISTS kg_node_terms (
                    term_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    term_norm TEXT NOT NULL,
                    term_raw TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    role TEXT NOT NULL,
                    scope_ref TEXT NOT NULL,
                    is_exact_code INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS kg_edges (
                    edge_id TEXT NOT NULL,
                    scope_ref TEXT NOT NULL,
                    src_id TEXT NOT NULL,
                    dst_id TEXT NOT NULL,
                    rel_type TEXT NOT NULL,
                    weight REAL NOT NULL DEFAULT 1.0,
                    qualifiers_json TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    evidence_refs_json TEXT NOT NULL,
                    source_tables_json TEXT NOT NULL,
                    observations_json TEXT NOT NULL,
                    PRIMARY KEY (edge_id, scope_ref)
                );
                CREATE INDEX IF NOT EXISTS idx_terms_norm_scope ON kg_node_terms(term_norm, scope_ref);
                CREATE INDEX IF NOT EXISTS idx_terms_node ON kg_node_terms(node_id);
                CREATE INDEX IF NOT EXISTS idx_edges_out ON kg_edges(src_id, rel_type, dst_id);
                CREATE INDEX IF NOT EXISTS idx_edges_in ON kg_edges(dst_id, rel_type, src_id);
                CREATE INDEX IF NOT EXISTS idx_node_view_scope ON kg_node_scope_views(scope_ref, node_id);
                """
            )

    def replace_index(
        self,
        *,
        nodes: list[dict[str, Any]],
        scope_views: list[dict[str, Any]],
        terms: list[dict[str, Any]],
        edges: list[dict[str, Any]],
        relation_definitions=(),
    ) -> None:
        with closing(self._connect()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                conn.execute("""CREATE TABLE IF NOT EXISTS kg_relation_semantics (
                    rel_type TEXT NOT NULL, src_kind TEXT NOT NULL, dst_kind TEXT NOT NULL,
                    orientation TEXT NOT NULL, PRIMARY KEY (rel_type, src_kind, dst_kind, orientation)
                )""")
                conn.execute("""CREATE TABLE IF NOT EXISTS kg_endpoint_specializations (
                    base_type TEXT NOT NULL, rel_type TEXT NOT NULL, PRIMARY KEY (base_type, rel_type)
                )""")
                for table in ('kg_endpoint_specializations', 'kg_relation_semantics', 'kg_edges', 'kg_node_terms',
                              'kg_node_scope_views', 'kg_nodes'):
                    conn.execute(f'DELETE FROM {table}')
                relation_definitions = tuple(relation_definitions)
                conn.executemany('INSERT INTO kg_endpoint_specializations VALUES (?,?)',
                                 _endpoint_specializations(relation_definitions))
                conn.executemany('INSERT OR IGNORE INTO kg_relation_semantics VALUES (?,?,?,?)', (
                    (d['type'], d['source_type'], d['target_type'], d.get('hierarchy_orientation') or 'undeclared')
                    for d in relation_definitions if all(d.get(k) for k in ('type', 'source_type', 'target_type'))))
                conn.executemany(
                    'INSERT INTO kg_nodes (node_id, kind, identity_json, identity_types_json) VALUES (?, ?, ?, ?)',
                    [
                        (n['node_id'], n['kind'], json.dumps(n['identity'], ensure_ascii=False),
                         json.dumps(n.get('identity_types') or {}, ensure_ascii=False))
                        for n in nodes
                    ],
                )
                conn.executemany(
                    'INSERT INTO kg_node_scope_views (node_id, scope_ref, chinese_name, english_name, '
                    'display_name, aliases_json, description, source_tables_json, scopes_json, observations_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    [
                        (v['node_id'], v['scope_ref'], v.get('chinese_name'), v.get('english_name'),
                         v['display_name'], json.dumps(v.get('aliases', []), ensure_ascii=False),
                         v.get('description'), json.dumps(v.get('source_tables', []), ensure_ascii=False),
                         json.dumps(v['scopes'], ensure_ascii=False), json.dumps(v.get('observations', []), ensure_ascii=False))
                        for v in scope_views
                    ],
                )
                conn.executemany(
                    'INSERT INTO kg_node_terms (term_norm, term_raw, node_id, kind, role, scope_ref, is_exact_code) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?)',
                    [
                        (t['term_norm'], t['term_raw'], t['node_id'], t['kind'],
                         t['role'], t['scope_ref'], 1 if t.get('is_exact_code') else 0)
                        for t in terms
                    ],
                )
                conn.executemany(
                    'INSERT INTO kg_edges (edge_id, scope_ref, src_id, dst_id, rel_type, weight, qualifiers_json, '
                    'scopes_json, evidence_refs_json, source_tables_json, observations_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    [
                        (e['edge_id'], e['scope_ref'], e['src_id'], e['dst_id'], e['rel_type'], float(e.get('weight', 1.0)),
                         json.dumps(e.get('qualifiers', {}), ensure_ascii=False),
                         json.dumps(e.get('scopes', []), ensure_ascii=False),
                         json.dumps(e.get('evidence_refs', []), ensure_ascii=False),
                         json.dumps(e.get('source_tables', []), ensure_ascii=False),
                         json.dumps(e.get('observations', []), ensure_ascii=False))
                        for e in edges
                    ],
                )
                conn.execute('COMMIT')
            except BaseException:
                conn.execute('ROLLBACK')
                raise

    def authorized_kinds(self, scopes):
        tokens = sorted(set(scopes))
        if not tokens:
            return []
        marks = ','.join('?' for _ in tokens)
        with closing(self._connect()) as conn:
            return [row[0] for row in conn.execute(
                f'SELECT DISTINCT n.kind FROM kg_nodes n JOIN kg_node_scope_views v ON v.node_id=n.node_id '
                f'WHERE v.scope_ref IN ({marks}) ORDER BY n.kind', tokens)]

    def authorized_relation_signatures(self, scopes):
        tokens = sorted(set(scopes))
        if not tokens:
            return []
        marks = ','.join('?' for _ in tokens)
        with closing(self._connect()) as conn:
            return list(conn.execute(
                f'SELECT DISTINCT a.kind, e.rel_type, b.kind FROM ({self._authorized_edges_sql(marks)}) e '
                'JOIN kg_nodes a ON a.node_id=e.src_id JOIN kg_nodes b ON b.node_id=e.dst_id',
                tokens * 3))

    def authorized_hierarchies(self, scopes):
        return self.hierarchy_directions(scopes)[0]

    def hierarchy_directions(self, scopes, *, anchor=None):
        """Return directions only for type signatures with a visible edge and visible endpoints."""
        tokens = sorted(set(scopes))
        if not tokens:
            return {}, {}
        marks = ','.join('?' for _ in tokens)
        orientations_by_signature = defaultdict(set)
        with closing(self._connect()) as conn:
            # Existing immutable format-5 indexes predate orientation metadata.
            has_semantics = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='kg_relation_semantics'").fetchone()
            if anchor is not None and not has_semantics:
                raise LightGraphError('retrieval_index_stale')
            field = 'sem.orientation' if has_semantics else 'NULL'
            join = (' LEFT JOIN kg_relation_semantics sem ON sem.rel_type=e.rel_type '
                    'AND sem.src_kind=a.kind AND sem.dst_kind=b.kind') if has_semantics else ''
            incident = ' WHERE e.src_id=? OR e.dst_id=?' if anchor is not None else ''
            rows = conn.execute(
                f'SELECT DISTINCT a.kind, e.rel_type, b.kind, {field} FROM ({self._authorized_edges_sql(marks)}) e '
                f'JOIN kg_nodes a ON a.node_id=e.src_id JOIN kg_nodes b ON b.node_id=e.dst_id{join}{incident}',
                tokens * 3 + ([anchor, anchor] if anchor is not None else []))
            for src, rel, dst, orientation in rows:
                orientations_by_signature[src, rel, dst].add(orientation)
        result, mapped = {}, {}
        for signature, orientations in orientations_by_signature.items():
            if orientations == {None} or orientations == {'undeclared'}:
                # Compatibility for the explicit legacy parent/child predicates.
                # Generic containment, prose labels, and acyclic topology don't
                # distinguish classification, composition and other associations.
                legacy = signature[1].casefold().replace('-', '_').replace(' ', '_')
                if anchor is not None:
                    flow = mapped_direction(signature[1])
                    if orientations == {'undeclared'} and flow:
                        mapped[signature] = flow
                elif legacy in {'has_child', 'has_children', 'parent_of'}:
                    orientations = {'parent_to_child'}
                elif legacy in {'child_of', 'has_parent'}:
                    orientations = {'child_to_parent'}
            if orientations == {'parent_to_child'}:
                result[signature] = 'outgoing'
            elif orientations == {'child_to_parent'}:
                result[signature] = 'incoming'
        return result, mapped

    def authorized_relations(self, scopes):
        tokens = sorted(set(scopes))
        if not tokens:
            return []
        marks = ','.join('?' for _ in tokens)
        with closing(self._connect()) as conn:
            return [row[0] for row in conn.execute(
                f'SELECT DISTINCT rel_type FROM ({self._authorized_edges_sql(marks)}) ORDER BY rel_type',
                tokens * 3)]

    def expand_endpoint_relation_types(self, names, scopes):
        """Keep exact filters; add endpoint specializations backed by a visible edge and endpoints."""
        requested = sorted(set(names))
        if not requested:
            return []
        tokens = sorted(set(scopes))
        with closing(self._connect()) as conn:
            has_specializations = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kg_endpoint_specializations'").fetchone()
            if not has_specializations:
                raise LightGraphError('retrieval_index_stale')
            if not tokens:
                return requested
            scopes_sql = ','.join('?' for _ in tokens)
            names_sql = ','.join('?' for _ in requested)
            matches = conn.execute(
                f'SELECT DISTINCT e.rel_type FROM ({self._authorized_edges_sql(scopes_sql)}) e '
                f'JOIN kg_endpoint_specializations s ON s.rel_type=e.rel_type WHERE s.base_type IN ({names_sql})',
                [*tokens, *tokens, *tokens, *requested])
            return sorted(set(requested).union(row[0] for row in matches))

    def authorized_terms(self, scopes: Sequence[str] | set[str], *, field: str = 'all') -> list[str]:
        scope_list = sorted({str(s) for s in scopes})
        if not scope_list or field == 'description':
            return []
        placeholders = ','.join('?' for _ in scope_list)
        roles = tuple(role for role in _TERM_ROLES[field] if role != 'description')
        role_marks = ','.join('?' for _ in roles)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f'SELECT DISTINCT term_raw FROM kg_node_terms '
                f'WHERE scope_ref IN ({placeholders}) AND role IN ({role_marks})',
                [*scope_list, *roles],
            ).fetchall()
        return [str(r['term_raw']) for r in rows if r['term_raw']]

    def search_lexical(
        self,
        surface: str,
        *,
        scopes: Sequence[str] | set[str],
        limit: int = 20,
        field: str = 'all',
    ) -> list[dict[str, Any]]:
        norm = normalize_term(surface)
        scope_list = sorted({str(s) for s in scopes})
        if not norm or not scope_list:
            return []
        placeholders = ','.join('?' for _ in scope_list)
        if field == 'description':
            # Keep a sentence intact for semantic ranking. The lexical branch
            # also recognizes explicit terms separated by whitespace or punctuation.
            words = list(dict.fromkeys(re.findall(r'[\w]+', norm)))[:32] or [norm]
            word_marks = ','.join('(?)' for _ in words)
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    f"""
                    WITH words(word) AS (VALUES {word_marks})
                    SELECT node_id, kind, MAX(CASE
                        WHEN term_norm = ? THEN 3.0
                        WHEN instr(term_norm, ?) > 0 THEN 2.0
                        ELSE (SELECT COUNT(*) FROM words WHERE instr(term_norm, word) > 0) * 1.0 / ?
                    END) AS score
                    FROM kg_node_terms WHERE scope_ref IN ({placeholders}) AND role = 'description'
                    GROUP BY node_id, kind HAVING score > 0 ORDER BY score DESC, node_id LIMIT ?
                    """,
                    [*words, norm, norm, len(words), *scope_list, limit],
                ).fetchall()
            return [{'node_id': row['node_id'], 'kind': row['kind'], 'score': row['score'],
                     'match_type': 'exact_description' if row['score'] == 3 else 'description_keywords'}
                    for row in rows]
        roles = _TERM_ROLES[field]
        role_marks = ','.join('?' for _ in roles)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT node_id, kind, role, scope_ref, is_exact_code, term_norm,
                    CASE
                        WHEN term_norm = ? AND is_exact_code = 1 THEN 4.0
                        WHEN term_norm = ? THEN 3.0
                        WHEN instr(term_norm, ?) = 1 THEN 2.0
                        ELSE 1.0
                    END AS match_score
                FROM kg_node_terms
                WHERE scope_ref IN ({placeholders})
                  AND role IN ({role_marks})
                  AND (term_norm = ? OR instr(term_norm, ?) > 0 OR instr(?, term_norm) > 0)
                """,
                [norm, norm, norm, *scope_list, *roles, norm, norm, norm],
            ).fetchall()

        best_by_node: dict[str, dict[str, Any]] = {}
        for row in rows:
            nid = row['node_id']
            # Require reasonable overlap for reverse containment to avoid 1-char false hits.
            tnorm = row['term_norm']
            if tnorm != norm and norm not in tnorm and len(tnorm) < 2:
                continue
            score = float(row['match_score'])
            if tnorm == norm:
                mtype = ('exact_code' if row['is_exact_code'] else
                         'exact_description' if row['role'] == 'description' else 'exact_name')
            elif tnorm.startswith(norm):
                mtype = 'prefix'
            else:
                mtype = 'substring'
            prev = best_by_node.get(nid)
            if prev is None or score > prev['score']:
                best_by_node[nid] = {
                    'node_id': nid,
                    'kind': row['kind'],
                    'score': score,
                    'match_type': mtype,
                    'is_exact_code': bool(row['is_exact_code'] and tnorm == norm),
                    'is_exact_name': mtype == 'exact_name',
                }
        return sorted(best_by_node.values(), key=lambda x: (-x['score'], x['node_id']))[:limit]

    def source_passages(self, surfaces, *, scopes, limit):
        """Return text from the exact permitted observation, without asserting entity identity."""
        tokens = sorted(set(scopes))
        if not tokens or not surfaces or limit <= 0:
            return []
        marks = ','.join('?' for _ in tokens)
        excerpts = []
        with closing(self._connect()) as conn:
            for text in dict.fromkeys(surfaces):
                norm = normalize_term(text)
                if not norm:
                    continue
                rows = conn.execute(f"""
                    SELECT t.node_id, t.role, v.display_name, v.observations_json
                    FROM kg_node_terms t JOIN kg_node_scope_views v
                      ON v.node_id = t.node_id AND v.scope_ref = t.scope_ref
                    WHERE t.scope_ref IN ({marks}) AND t.role IN ('description','source_property')
                      AND instr(t.term_norm, ?) > 0
                    GROUP BY t.node_id, t.scope_ref, t.role
                    ORDER BY t.node_id, t.scope_ref, t.role LIMIT ?
                    """, [*tokens, norm, limit - len(excerpts)])
                for row in rows:
                    for obs in json.loads(row['observations_json']):
                        values = ([(None, obs.get('description'))] if row['role'] == 'description'
                                  else observed_text_properties(obs))
                        seen = set()
                        for name, raw in values:
                            if not isinstance(raw, str) or norm not in normalize_term(raw):
                                continue
                            if (name, raw) in seen:
                                continue
                            seen.add((name, raw))
                            excerpts.append({'text': text, 'node_id': row['node_id'], 'display_name': row['display_name'],
                                             'excerpt': _source_excerpt(raw, text, norm),
                                             'field': 'property' if name is not None else 'description',
                                             **({'property': name} if name is not None else {}),
                                             'observation_id': obs.get('observation_id'),
                                             'evidence_refs': obs.get('evidence_refs', []),
                                             'source_tables': obs.get('source_tables', [])})
                            if len(excerpts) >= limit:
                                return excerpts
        return excerpts

    def authorized_labels(self, node_ids, *, scopes):
        """Read candidate labels without loading observations or counting hub neighbors."""
        ids, scope_list = sorted(set(node_ids)), sorted(set(scopes))
        if not ids or not scope_list:
            return {}
        id_marks = ','.join('?' for _ in ids)
        scope_marks = ','.join('?' for _ in scope_list)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""SELECT node_id, COALESCE(MIN(NULLIF(chinese_name,'')), MIN(NULLIF(display_name,'')), node_id)
                    FROM kg_node_scope_views
                    WHERE node_id IN ({id_marks}) AND scope_ref IN ({scope_marks}) GROUP BY node_id""",
                [*ids, *scope_list],
            )
            return dict(rows)

    def exact_names(self, terms, *, scopes, limit_per_term, include_codes=False, kind=None):
        """Resolve visible names and aliases, optionally also exact identity codes."""
        terms = sorted({normalize_term(term) for term in terms if normalize_term(term)})
        scopes = sorted(set(scopes))
        if not terms or not scopes:
            return {}
        scope_marks = ','.join('?' for _ in scopes)
        roles = (*_NAME_ROLES, 'identity_code') if include_codes else _NAME_ROLES
        role_marks = ','.join('?' for _ in roles)
        filters = [] if kind is None else [kind]
        sql = f"""SELECT node_id, kind FROM kg_node_terms
                  WHERE term_norm = ? AND scope_ref IN ({scope_marks}) AND role IN ({role_marks})
                    {'' if include_codes else "AND (role != 'display_name' OR term_raw != node_id)"}
                    {'AND kind = ?' if filters else ''}
                  GROUP BY node_id, kind ORDER BY MAX(is_exact_code) DESC, node_id LIMIT ?"""
        with closing(self._connect()) as conn:
            return {term: [dict(row) for row in conn.execute(
                sql, (term, *scopes, *roles, *filters, max(2, limit_per_term)))] for term in terms}

    def load_authorized_nodes(
        self,
        node_ids: Sequence[str] | set[str],
        *,
        scopes: Sequence[str] | set[str],
    ) -> dict[str, dict[str, Any]]:
        ids = sorted({str(nid) for nid in node_ids})
        scope_list = sorted({str(s) for s in scopes})
        if not ids or not scope_list:
            return {}
        id_ph = ','.join('?' for _ in ids)
        sc_ph = ','.join('?' for _ in scope_list)
        # Both endpoints must be visible, even if the relationship has a
        # separate grant. Count each visible edge once across overlapping views.
        authorized_edges = self._authorized_edges_sql(sc_ph)
        with closing(self._connect()) as conn:
            degrees = conn.execute(
                f'WITH authorized_edges AS ({authorized_edges}) '
                f'SELECT id, COUNT(*) AS degree FROM ('
                f'SELECT DISTINCT src_id AS id, edge_id FROM authorized_edges WHERE src_id IN ({id_ph}) UNION ALL '
                f'SELECT DISTINCT dst_id AS id, edge_id FROM authorized_edges WHERE dst_id IN ({id_ph})) GROUP BY id',
                [*scope_list, *scope_list, *scope_list, *ids, *ids],
            )
            degrees_by_node = {row['id']: row['degree'] for row in degrees}
            base_rows = conn.execute(
                f'SELECT node_id, kind, identity_json, identity_types_json FROM kg_nodes WHERE node_id IN ({id_ph})',
                ids,
            ).fetchall()
            view_rows = conn.execute(
                f'SELECT node_id, scope_ref, chinese_name, english_name, display_name, aliases_json, description, source_tables_json, scopes_json, observations_json '
                f'FROM kg_node_scope_views WHERE node_id IN ({id_ph}) AND scope_ref IN ({sc_ph})',
                [*ids, *scope_list],
            ).fetchall()
        views_by_node: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for v in view_rows:
            views_by_node[v['node_id']].append(v)

        result: dict[str, dict[str, Any]] = {}
        for b in base_rows:
            nid = b['node_id']
            vlist = views_by_node.get(nid)
            if not vlist:
                continue
            cn_names = sorted({v['chinese_name'] for v in vlist if v['chinese_name']})
            en_names = sorted({v['english_name'] for v in vlist if v['english_name']})
            disp_names = sorted({v['display_name'] for v in vlist if v['display_name']})
            aliases = sorted({a for v in vlist for a in json.loads(v['aliases_json'])})
            descs = sorted({v['description'] for v in vlist if v['description']})
            src_tables = sorted({t for v in vlist for t in json.loads(v['source_tables_json'])})
            observations = {canonical(item): item for view in vlist for item in json.loads(view['observations_json'])}
            result[nid] = {
                'id': nid,
                'kind': b['kind'],
                'degree': degrees_by_node.get(nid, 0),
                'identity': json.loads(b['identity_json']),
                'identity_types': json.loads(b['identity_types_json']),
                'chinese_name': cn_names[0] if cn_names else None,
                'english_name': en_names[0] if en_names else None,
                'display_name': cn_names[0] if cn_names else (disp_names[0] if disp_names else nid),
                'aliases': aliases,
                'description': '；'.join(descs) if descs else None,
                'source_tables': src_tables,
                'scopes': sorted({scope for view in vlist for scope in json.loads(view['scopes_json'])}),
                'observations': [observations[key] for key in sorted(observations)],
            }
        return result

    @staticmethod
    def _authorized_edges_sql(sc_ph: str) -> str:
        return f"""
            SELECT e.* FROM kg_edges e WHERE e.scope_ref IN ({sc_ph})
            AND EXISTS (SELECT 1 FROM kg_node_scope_views v WHERE v.node_id=e.src_id AND v.scope_ref IN ({sc_ph}))
            AND EXISTS (SELECT 1 FROM kg_node_scope_views v WHERE v.node_id=e.dst_id AND v.scope_ref IN ({sc_ph}))
        """

    def _route_rows(self, sql, params, truncation):
        with closing(self._connect()) as conn:
            conn.set_progress_handler(lambda: 1, _ROUTE_SQL_STEPS)
            try:
                return [dict(row) for row in conn.execute(sql, params)]
            except sqlite3.OperationalError as error:
                if getattr(error, 'sqlite_errorcode', None) != sqlite3.SQLITE_INTERRUPT or truncation is None:
                    raise
                truncation.add('association_sql_work_limit')
                return []

    def grouping_routes(self, seed, surface, *, scopes, subject_term=None, suggested_ids=(), limit=16, max_depth=2,
                        truncation=None):
        """Find nearby axis names for every visible interpretation before limiting routes."""
        seeds = [seed] if isinstance(seed, str) else list(seed)
        tokens = sorted(set(scopes))
        if not tokens or not surface:
            return []
        marks = ','.join('?' for _ in tokens)
        names = ','.join('?' for _ in _NAME_ROLES)
        norm = normalize_term(surface)
        # Compute adjacency from small endpoint records; observations are verified later.
        # When a subject has many exact matches, its output shortlist is not its search space.
        sql = f"""
            WITH subjects(seed) AS (
                SELECT value FROM json_each(?)
                UNION SELECT node_id FROM kg_node_terms WHERE term_norm=? AND scope_ref IN ({marks})
                      AND role IN ({names},'identity_code')
            ), visible AS (
                SELECT DISTINCT src_id, dst_id FROM ({self._authorized_edges_sql(marks)})
            ), adjacency(src,dst) AS (
                SELECT src_id,dst_id FROM visible UNION ALL SELECT dst_id,src_id FROM visible
            ), adjacent AS (SELECT DISTINCT seed,dst FROM subjects JOIN adjacency ON src=seed AND dst!=seed),
            matches AS (
                SELECT node_id, MIN(CASE WHEN term_norm=? THEN 0 ELSE 1 END) AS rank
                FROM kg_node_terms WHERE scope_ref IN ({marks}) AND role IN ({names}) AND instr(term_norm,?)>0
                GROUP BY node_id
                UNION ALL SELECT DISTINCT node_id,2 FROM kg_node_terms
                          WHERE scope_ref IN ({marks}) AND instr(lower(kind),?)>0
                UNION ALL SELECT value,3 FROM json_each(?)
            ), routes(seed,target,bridge,rank) AS (
                SELECT i.seed,i.dst,NULL,m.rank FROM adjacent i JOIN matches m ON m.node_id=i.dst
                UNION ALL SELECT i.seed,m.node_id,i.dst,m.rank FROM adjacent i
                    JOIN adjacency a ON a.src=i.dst JOIN matches m ON m.node_id=a.dst
                    WHERE ? >= 2 AND a.dst!=i.seed AND a.dst!=i.dst
            ), ranked AS (
                SELECT seed,target,bridge,MIN(rank) AS rank,
                       ROW_NUMBER() OVER (PARTITION BY seed ORDER BY MIN(rank),bridge IS NOT NULL,target,bridge) AS n
                FROM routes GROUP BY seed,target,bridge
            )
            SELECT seed,target,bridge,rank FROM ranked ORDER BY rank,n,bridge IS NOT NULL,seed,target,bridge LIMIT ?
        """
        return self._route_rows(sql, [
            json.dumps(seeds), normalize_term(subject_term) if subject_term else None,
            *tokens, *_NAME_ROLES, *tokens, *tokens, *tokens, norm,
            *tokens, *_NAME_ROLES, norm, *tokens, norm, json.dumps(list(suggested_ids)), max_depth, limit,
        ], truncation)

    def exact_association_links(self, terms, *, scopes, limit, truncation=None):
        """Rank links between exact operands using their authorized adjacency before the display cutoff."""
        tokens = sorted(set(scopes))
        terms = sorted({normalize_term(term) for term in terms})
        if not tokens or len(terms) < 2:
            return []
        marks = ','.join('?' for _ in tokens)
        sql = f"""
            WITH matches(term,id,kind) AS (
                SELECT DISTINCT term_norm,node_id,kind FROM kg_node_terms
                WHERE term_norm IN (SELECT value FROM json_each(?))
                  AND scope_ref IN ({marks}) AND role IN (SELECT value FROM json_each(?))
            ), pairs AS (
                SELECT DISTINCT l.term lt,l.id src,l.kind sk,r.term rt,r.id dst,r.kind dk
                FROM matches l JOIN kg_edges e ON e.src_id=l.id JOIN matches r ON r.id=e.dst_id
                WHERE l.term!=r.term AND l.id!=r.id AND e.scope_ref IN ({marks})
            ), links AS (
                SELECT * FROM pairs UNION SELECT rt,dst,dk,lt,src,sk FROM pairs
            ), coverage AS (
                SELECT src,lt,COUNT(DISTINCT rt) AS terms FROM links GROUP BY src,lt
            ), ranked AS (
                SELECT l.*,c.terms,ROW_NUMBER() OVER (
                    PARTITION BY lt,sk,rt,dk ORDER BY c.terms DESC,src,dst
                ) AS n FROM links l JOIN coverage c USING (src,lt)
            )
            SELECT src,lt,sk,dst,rt,dk FROM ranked ORDER BY terms DESC,n,lt,rt,src,dst LIMIT ?
        """
        return self._route_rows(sql, [
            json.dumps(terms), *tokens, json.dumps([*_NAME_ROLES, 'identity_code']), *tokens, limit,
        ], truncation)

    def expand_neighborhood(
        self,
        seed_ids: Sequence[str],
        *,
        scopes: Sequence[str] | set[str],
        hops: int = 2,
        max_per_rel: int = 30,
        hub_degree_limit: int = 150,
        max_nodes: int = 80,
        max_edges: int = 150,
        relation_types: Sequence[str] | None = None,
        direction: str = 'both',
        preferred_neighbor_ids: Sequence[str] = (),
        allowed_neighbor_ids: Sequence[str] | None = None,
        hierarchies: dict[tuple[str, str, str], str] | None = None,
        mapped_hierarchies: dict[tuple[str, str, str], str] | None = None,
    ) -> dict[str, Any]:
        scope_list = sorted({str(s) for s in scopes})
        if not seed_ids or not scope_list:
            return {'nodes': {}, 'edges': [], 'truncated': False, 'truncation_reasons': [], 'hub_skipped_nodes': []}

        authorized_seeds = self.load_authorized_nodes(seed_ids[:max_nodes], scopes=scope_list)
        if not authorized_seeds:
            return {'nodes': {}, 'edges': [], 'truncated': False, 'truncation_reasons': [], 'hub_skipped_nodes': []}

        nodes = dict(authorized_seeds)
        edges_by_id: dict[str, dict[str, Any]] = {}
        truncation_reasons = {'max_nodes_reached'} if len(seed_ids) > max_nodes else set()
        hub_skipped: set[str] = set()
        sc_ph = ','.join('?' for _ in scope_list)
        if direction not in {'both', 'outgoing', 'incoming'}:
            raise ValueError('invalid_traversal_direction')
        relations = sorted(set(relation_types or []))
        rel_sql = ' AND rel_type IN (' + ','.join('?' for _ in relations) + ')' if relations else ''
        preferred = sorted(set(preferred_neighbor_ids))

        frontier = list(authorized_seeds.keys())
        visited_expanded: set[str] = set()

        with closing(self._connect()) as conn:
            if mapped_hierarchies:
                conn.create_function('corroborated_level_keys', 2, corroborated_level_keys, deterministic=True)
            for hop in range(1, hops + 1):
                if not frontier:
                    break
                if len(edges_by_id) >= max_edges:
                    truncation_reasons.add('subgraph_budget_reached')
                    break
                next_frontier_candidates: set[str] = set()
                # Filter hubs on hop >= 2 to prevent super-hub explosion.
                active_frontier = []
                for fid in frontier:
                    if fid in visited_expanded:
                        continue
                    visited_expanded.add(fid)
                    deg = nodes[fid]['degree']
                    if hop >= 2 and deg > hub_degree_limit:
                        hub_skipped.add(fid)
                        truncation_reasons.add('hub_degree_capped')
                        continue
                    active_frontier.append(fid)
                if not active_frontier:
                    break

                f_ph = ','.join('?' for _ in active_frontier)
                frontier_tests, frontier_params = [], []
                allowed = sorted(set(allowed_neighbor_ids)) if allowed_neighbor_ids is not None else None
                if allowed == []:
                    break
                for src, dst, flow in [('src_id', 'dst_id', 'outgoing'), ('dst_id', 'src_id', 'incoming')]:
                    if direction not in {'both', flow}:
                        continue
                    condition = f'{src} IN ({f_ph})'
                    frontier_params.extend(active_frontier)
                    if allowed is not None:
                        marks = ','.join('?' for _ in allowed)
                        condition += f' AND {dst} IN ({marks})'
                        frontier_params.extend(allowed)
                    if hierarchies is not None:
                        # Select the exact typed predicate and its parent endpoint
                        # before limits, including when successive levels reverse
                        # their physical edge orientation.
                        typed_predicate = """(
                            (SELECT kind FROM kg_nodes WHERE node_id=src_id), rel_type,
                            (SELECT kind FROM kg_nodes WHERE node_id=dst_id)
                        ) IN (SELECT json_extract(value,'$[0]'), json_extract(value,'$[1]'),
                                    json_extract(value,'$[2]') FROM json_each(?))"""
                        predicates = [typed_predicate]
                        frontier_params.append(json.dumps([key for key in hierarchies if hierarchies[key] == flow]))
                        if mapped_hierarchies:
                            predicates.append(f'({typed_predicate} AND corroborated_level_keys(rel_type, observations_json))')
                            frontier_params.append(json.dumps([key for key in mapped_hierarchies if mapped_hierarchies[key] == flow]))
                        condition += ' AND (' + ' OR '.join(predicates) + ')'
                        if mapped_hierarchies is not None:
                            condition += ' AND src_id != dst_id'
                    frontier_tests.append(f'({condition})')
                frontier_sql = ' OR '.join(frontier_tests)
                priority_sql, priority_params = '', []
                if preferred:
                    # Rank direct connections to explicit targets among the
                    # authorized, direction-matching edges before applying fanout.
                    p_ph = ','.join('?' for _ in preferred)
                    if direction == 'both':
                        priority_test = (f'(src_id IN ({f_ph}) AND dst_id IN ({p_ph})) OR '
                                         f'(dst_id IN ({f_ph}) AND src_id IN ({p_ph}))')
                        priority_params = [*active_frontier, *preferred, *active_frontier, *preferred]
                    else:
                        neighbor = 'dst_id' if direction == 'outgoing' else 'src_id'
                        priority_test = f'{neighbor} IN ({p_ph})'
                        priority_params = preferred
                    priority_sql = f'CASE WHEN {priority_test} THEN 0 ELSE 1 END, '
                # Merge only the caller's visible evidence when a logical edge
                # has supports from multiple scopes.
                raw_edges = conn.execute(
                    f'WITH authorized AS ({self._authorized_edges_sql(sc_ph)}) '
                    f'SELECT * FROM authorized WHERE ({frontier_sql}){rel_sql} '
                    f'ORDER BY {priority_sql}weight DESC, edge_id, scope_ref',
                    [*scope_list, *scope_list, *scope_list, *frontier_params,
                     *relations, *priority_params],
                )
                per_rel_counts: dict[tuple[str, str, str], int] = defaultdict(int)
                candidate_edges = []
                retained_endpoints = set(nodes)
                for eid, versions in groupby(raw_edges, key=lambda record: record['edge_id']):
                    if eid in edges_by_id:
                        continue
                    row = next(versions)
                    src, dst, rel = row['src_id'], row['dst_id'], row['rel_type']
                    admitted = False
                    if direction != 'incoming' and src in active_frontier:
                        key = (src, rel, 'out')
                        if per_rel_counts[key] < max_per_rel:
                            per_rel_counts[key] += 1
                            admitted = True
                        else:
                            truncation_reasons.add('per_relation_limit_reached')
                    if direction != 'outgoing' and dst in active_frontier:
                        key = (dst, rel, 'in')
                        if per_rel_counts[key] < max_per_rel:
                            per_rel_counts[key] += 1
                            admitted = True
                        else:
                            truncation_reasons.add('per_relation_limit_reached')
                    if not admitted:
                        continue
                    new_ids = {src, dst} - retained_endpoints
                    if len(retained_endpoints) + len(new_ids) > max_nodes:
                        truncation_reasons.add('max_nodes_reached')
                        continue
                    if len(edges_by_id) + len(candidate_edges) >= max_edges:
                        truncation_reasons.add('max_edges_reached')
                        break
                    retained_endpoints.update(new_ids)
                    qualifiers = defaultdict(list)
                    refs, tables = set(), set()
                    observations = {}
                    for record in chain((row,), versions):
                        refs.update(json.loads(record['evidence_refs_json']))
                        tables.update(json.loads(record['source_tables_json']))
                        observations.update({canonical(item): item for item in json.loads(record['observations_json'])})
                        for key, values in json.loads(record['qualifiers_json']).items():
                            for value in values:
                                if value not in qualifiers[key]:
                                    qualifiers[key].append(value)
                    candidate_edges.append({**dict(row), 'evidence_refs': refs,
                                            'source_tables': tables, 'qualifiers': dict(qualifiers),
                                            'observations': [observations[key] for key in sorted(observations)]})
                fetched = self.load_authorized_nodes(retained_endpoints - set(nodes), scopes=scope_list)
                for row in candidate_edges:
                    src, dst = row['src_id'], row['dst_id']
                    if src not in nodes:
                        nodes[src] = fetched[src]
                        next_frontier_candidates.add(src)
                    if dst not in nodes:
                        nodes[dst] = fetched[dst]
                        next_frontier_candidates.add(dst)
                    eid = row['edge_id']
                    if eid not in edges_by_id:
                        edges_by_id[eid] = {
                            'id': eid,
                            'source': src,
                            'target': dst,
                            'relation': row['rel_type'],
                            'weight': float(row['weight']),
                            'qualifiers': row['qualifiers'],
                            'evidence_refs': sorted(row['evidence_refs']),
                            'source_tables': sorted(row['source_tables']),
                            'observations': row['observations'],
                        }
                frontier = sorted(next_frontier_candidates)

        return {
            'nodes': nodes,
            'edges': sorted(edges_by_id.values(), key=lambda e: e['id']),
            'truncated': bool(truncation_reasons),
            'truncation_reasons': sorted(truncation_reasons),
            'hub_skipped_nodes': sorted(hub_skipped),
        }
