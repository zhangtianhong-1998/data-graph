"""Local SQLite relational graph store with scope-bound terms and bounded BFS expansion."""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from itertools import chain, groupby
import json
from pathlib import Path
import sqlite3
from typing import Any, Sequence
import unicodedata


def normalize_term(value: Any) -> str:
    return ' '.join(unicodedata.normalize('NFKC', str(value or '')).casefold().split())


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
                    identity_json TEXT NOT NULL
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
    ) -> None:
        with closing(self._connect()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                for table in ('kg_edges', 'kg_node_terms', 'kg_node_scope_views', 'kg_nodes'):
                    conn.execute(f'DELETE FROM {table}')
                conn.executemany(
                    'INSERT INTO kg_nodes (node_id, kind, identity_json) VALUES (?, ?, ?)',
                    [
                        (n['node_id'], n['kind'], json.dumps(n['identity'], ensure_ascii=False))
                        for n in nodes
                    ],
                )
                conn.executemany(
                    'INSERT INTO kg_node_scope_views (node_id, scope_ref, chinese_name, english_name, '
                    'display_name, aliases_json, description, source_tables_json, scopes_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    [
                        (v['node_id'], v['scope_ref'], v.get('chinese_name'), v.get('english_name'),
                         v['display_name'], json.dumps(v.get('aliases', []), ensure_ascii=False),
                         v.get('description'), json.dumps(v.get('source_tables', []), ensure_ascii=False),
                         json.dumps(v['scopes'], ensure_ascii=False))
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
                    'scopes_json, evidence_refs_json, source_tables_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    [
                        (e['edge_id'], e['scope_ref'], e['src_id'], e['dst_id'], e['rel_type'], float(e.get('weight', 1.0)),
                         json.dumps(e.get('qualifiers', {}), ensure_ascii=False),
                         json.dumps(e.get('scopes', []), ensure_ascii=False),
                         json.dumps(e.get('evidence_refs', []), ensure_ascii=False),
                         json.dumps(e.get('source_tables', []), ensure_ascii=False))
                        for e in edges
                    ],
                )
                conn.execute('COMMIT')
            except BaseException:
                conn.execute('ROLLBACK')
                raise

    def authorized_terms(self, scopes: Sequence[str] | set[str]) -> list[str]:
        scope_list = sorted({str(s) for s in scopes})
        if not scope_list:
            return []
        placeholders = ','.join('?' for _ in scope_list)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f'SELECT DISTINCT term_raw FROM kg_node_terms WHERE scope_ref IN ({placeholders})',
                scope_list,
            ).fetchall()
        return [str(r['term_raw']) for r in rows if r['term_raw']]

    def search_lexical(
        self,
        surface: str,
        *,
        scopes: Sequence[str] | set[str],
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        norm = normalize_term(surface)
        scope_list = sorted({str(s) for s in scopes})
        if not norm or not scope_list:
            return []
        placeholders = ','.join('?' for _ in scope_list)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT node_id, kind, role, scope_ref, is_exact_code, term_norm,
                    CASE
                        WHEN term_norm = ? AND is_exact_code = 1 THEN 4.0
                        WHEN term_norm = ? THEN 3.0
                        WHEN term_norm LIKE ? THEN 2.0
                        ELSE 1.0
                    END AS match_score
                FROM kg_node_terms
                WHERE scope_ref IN ({placeholders})
                  AND (term_norm = ? OR instr(term_norm, ?) > 0 OR instr(?, term_norm) > 0)
                """,
                [norm, norm, norm + '%', *scope_list, norm, norm, norm],
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
                mtype = 'exact_code' if row['is_exact_code'] else 'exact_name'
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
                    'is_exact_name': bool(tnorm == norm),
                }
        return sorted(best_by_node.values(), key=lambda x: (-x['score'], x['node_id']))[:limit]

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
                f'SELECT node_id, kind, identity_json FROM kg_nodes WHERE node_id IN ({id_ph})',
                ids,
            ).fetchall()
            view_rows = conn.execute(
                f'SELECT node_id, scope_ref, chinese_name, english_name, display_name, aliases_json, description, source_tables_json, scopes_json '
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
            result[nid] = {
                'id': nid,
                'kind': b['kind'],
                'degree': degrees_by_node.get(nid, 0),
                'identity': json.loads(b['identity_json']),
                'chinese_name': cn_names[0] if cn_names else None,
                'english_name': en_names[0] if en_names else None,
                'display_name': cn_names[0] if cn_names else (disp_names[0] if disp_names else nid),
                'aliases': aliases,
                'description': '；'.join(descs) if descs else None,
                'source_tables': src_tables,
                'scopes': sorted({scope for view in vlist for scope in json.loads(view['scopes_json'])}),
            }
        return result

    @staticmethod
    def _authorized_edges_sql(sc_ph: str) -> str:
        return f"""
            SELECT e.* FROM kg_edges e WHERE e.scope_ref IN ({sc_ph})
            AND EXISTS (SELECT 1 FROM kg_node_scope_views v WHERE v.node_id=e.src_id AND v.scope_ref IN ({sc_ph}))
            AND EXISTS (SELECT 1 FROM kg_node_scope_views v WHERE v.node_id=e.dst_id AND v.scope_ref IN ({sc_ph}))
        """

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

        frontier = list(authorized_seeds.keys())
        visited_expanded: set[str] = set()

        with closing(self._connect()) as conn:
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
                # Merge only the caller's visible evidence when a logical edge
                # has supports from multiple scopes.
                raw_edges = conn.execute(
                    f'WITH authorized AS ({self._authorized_edges_sql(sc_ph)}) '
                    f'SELECT * FROM authorized WHERE src_id IN ({f_ph}) OR dst_id IN ({f_ph}) '
                    'ORDER BY weight DESC, edge_id, scope_ref',
                    [*scope_list, *scope_list, *scope_list, *active_frontier, *active_frontier],
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
                    if src in active_frontier:
                        key = (src, rel, 'out')
                        if per_rel_counts[key] < max_per_rel:
                            per_rel_counts[key] += 1
                            admitted = True
                        else:
                            truncation_reasons.add('per_relation_limit_reached')
                    if dst in active_frontier:
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
                    for record in chain((row,), versions):
                        refs.update(json.loads(record['evidence_refs_json']))
                        tables.update(json.loads(record['source_tables_json']))
                        for key, values in json.loads(record['qualifiers_json']).items():
                            for value in values:
                                if value not in qualifiers[key]:
                                    qualifiers[key].append(value)
                    candidate_edges.append({**dict(row), 'evidence_refs': refs,
                                            'source_tables': tables, 'qualifiers': dict(qualifiers)})
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
                        }
                frontier = sorted(next_frontier_candidates)

        return {
            'nodes': nodes,
            'edges': sorted(edges_by_id.values(), key=lambda e: e['id']),
            'truncated': bool(truncation_reasons),
            'truncation_reasons': sorted(truncation_reasons),
            'hub_skipped_nodes': sorted(hub_skipped),
        }
