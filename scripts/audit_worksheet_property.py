"""Compare a mapped property on two original Excel checkpoints at specific physical rows."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re

from lightgraph.construction.checkpoints import load_checkpoint
from lightgraph.contracts import canonical
from lightgraph.errors import LightGraphError
from lightgraph.source_values import scalar_kind


def _worksheet_property(support, column, aliases):
    prov = support["provenance"]
    constants = prov.get("constant_properties") or {}
    if not aliases:
        return ("constant_name_only" if column in constants else "no_mapped_property"), None
    if any(name in constants for name in aliases):
        return "also_mapped_as_constant", None
    observed = support.get("properties") or {}
    if any(name not in observed for name in aliases):
        return "mapped_property_absent", None
    records = [observed[name] for name in aliases]
    if any(not isinstance(values, list) or not values for values in records):
        return "empty_or_invalid_property", None
    if any(len(values) != 1 for values in records):
        return "multiple_property_values", None
    if any(not isinstance(values[0], (str, bool, float, int)) for values in records):
        return "empty_or_invalid_property", None
    if any(values[0] == "" for values in records):
        return "empty_string_property_value", None
    values = {(scalar_kind(record[0]), canonical(record[0])) for record in records}
    if len(values) != 1:
        return "different_alias_values", None
    if len(aliases) > 1:
        return "multiple_aliases_same_value", None
    return "single_property_value", values.pop()


def audit_worksheet_property_evidence(left_graph, right_graph, *, address, snapshot, column, rows, edge_ids=(None, None)):
    """Inspect two original graphs, optionally restricted to their respective original edge IDs.

    address is (source_id, schema, table). Classify each support once per graph
    and count the actual mapped names, including multiple aliases on one support.
    Cross-checks count properties with one mapped name and one value per
    support and selected row. They compare the property's persisted scalar
    kind with the explicitly recorded type of the opposite row cell.
    The result contains only counts.
    """
    rows = set(rows)
    if (not re.fullmatch(r"[0-9a-f]{64}", snapshot) or not rows
            or any(type(row) is not int or not 1 <= row <= 1_048_576 for row in rows)
            or len(address) != 3 or not all(isinstance(s, str) and s for s in address)
            or not isinstance(column, str) or not column):
        raise ValueError("Expected full source address, workbook SHA-256, column and physical row numbers")
    reports, typed_rows, property_rows = [], [], []
    for graph, ids in zip((left_graph, right_graph), edge_ids, strict=True):
        ids = None if ids is None else set(ids)
        matched_ids, matched_rows = set(), set()
        counts = {key: Counter() for key in ("property_result", "mapping_names", "column_coordinate",
                                            "row_cell", "singleton_support_rows_vs_other_graph")}
        typed, singletons = defaultdict(set), []
        for edge in graph["edges"]:
            if ids is not None and edge["id"] not in ids:
                continue
            if ids is not None:
                matched_ids.add(edge["id"])
            for support in edge.get("supports", []):
                prov = support.get("provenance") or {}
                source = prov.get("source") or {}
                witness = support.get("source_row_witness") or {}
                if (tuple(source.get(k) for k in ("source_id", "schema", "table")) != tuple(address)
                        or witness.get("kind") != "excel_rows_v1" or witness.get("snapshot") != snapshot):
                    continue
                spans = witness.get("rows")
                if not isinstance(spans, list) or not spans:
                    raise ValueError("Invalid worksheet row ranges on an audited support")
                last = 0
                for span in spans:
                    if (not isinstance(span, list) or len(span) != 2 or any(type(n) is not int for n in span)
                            or not last < span[0] <= span[1] <= 1_048_576):
                        raise ValueError("Invalid worksheet row ranges on an audited support")
                    last = span[1]
                selected = {row for row in rows if any(lo <= row <= hi for lo, hi in spans)}
                if not selected:
                    continue
                matched_rows.update(selected)
                aliases = [name for name, col in (prov.get("property_columns") or {}).items() if col == column]
                counts["mapping_names"].update("physical_name" if name == column else "property_alias" for name in aliases)
                status, value = _worksheet_property(support, column, aliases)
                counts["property_result"][status] += 1
                ordinals = prov.get("source_column_ordinals") or {}
                number = ordinals.get(column)
                coordinate = "projected_with_ordinal"
                if column not in (prov.get("columns") or []):
                    coordinate = "not_projected"
                elif (type(number) is not int or not 1 <= number <= 16_384
                      or sum(n == number for n in ordinals.values()) != 1):
                    coordinate = "missing_or_ambiguous_ordinal"
                counts["column_coordinate"][coordinate] += 1
                if coordinate != "projected_with_ordinal":
                    number = None
                record = support.get("row_context") or {}
                values, kinds = record.get("values") or {}, record.get("value_types") or {}
                if column not in values:
                    cell = "no_row_cell"
                elif column not in (prov.get("context_columns") or ()) or column not in kinds:
                    cell = "missing_mapping_or_type"
                else:
                    raw, kind = values[column], kinds[column]
                    valid = isinstance(raw, (str, int, float, bool)) or raw is None
                    if valid and (scalar_kind(raw) == kind or
                                  (isinstance(raw, str) and kind in ("date", "datetime", "time", "decimal"))):
                        cell = "typed_row_cell"
                        for row in selected:
                            typed[row].add((number, kind, canonical(raw)))
                    else:
                        cell = "invalid_typed_cell"
                counts["row_cell"][cell] += 1
                if status == "single_property_value":
                    singletons.extend((row, number, value) for row in selected)
        if ids is not None and ids != matched_ids:
            raise ValueError("A selected edge ID does not occur in its input graph")
        reports.append({**counts, "requested_rows": len(rows), "matched_rows": len(matched_rows),
                        "matched_supports": sum(counts["property_result"].values())})
        typed_rows.append(typed)
        property_rows.append(singletons)
    for counts, singletons, opposite in zip(reports, property_rows, reversed(typed_rows), strict=True):
        for row, number, (property_kind, property_value) in singletons:
            cells = opposite.get(row, set())
            if not cells:
                status = "no_typed_cell"
            elif number is None or {n for n, _, _ in cells} != {number}:
                status = "unverified_column_coordinate"
            elif len(cells) != 1:
                status = "conflicting_typed_cells"
            else:
                _, row_kind, row_value = next(iter(cells))
                if row_value != property_value:
                    status = "different_value"
                elif row_kind != property_kind:
                    status = "serialized_value_matches_type_differs"
                else:
                    status = "scalar_kind_and_value_match"
            counts["singleton_support_rows_vs_other_graph"][status] += 1
    return dict(zip(("left", "right"), reports, strict=True))


def _checkpoint_graph(path):
    path = path.resolve(strict=True)
    if path.name != 'checkpoint.json':
        raise ValueError("请选择原始检查点的 checkpoint.json")
    record = json.loads(path.read_text(encoding='utf-8'))
    for ancestor in path.parents:
        if ancestor.name == 'checkpoints' and ancestor.parent / record['relative_path'] == path.with_name('graph.graph'):
            return load_checkpoint(ancestor.parent, record)['graph']
    raise ValueError("检查点目录与提交记录中的相对路径不一致")


def main():
    parser = argparse.ArgumentParser(description="只读核对两个原始检查点中指定边的 Excel 属性来源")
    parser.add_argument('left', type=Path, help="第一份 checkpoint.json")
    parser.add_argument('right', type=Path, help="第二份 checkpoint.json")
    parser.add_argument('--address', nargs=3, required=True, metavar=('SOURCE_ID', 'SCHEMA', 'TABLE'))
    parser.add_argument('--snapshot', required=True, help="原工作簿的完整 SHA-256")
    parser.add_argument('--column', required=True, help="工作表中的物理列名")
    parser.add_argument('--rows', nargs='+', type=int, required=True, help="原工作表行号")
    parser.add_argument('--left-edges', nargs='+', required=True, help="第一份检查点的原始边 ID")
    parser.add_argument('--right-edges', nargs='+', required=True, help="第二份检查点的原始边 ID")
    args = parser.parse_args()
    try:
        result = audit_worksheet_property_evidence(
            _checkpoint_graph(args.left), _checkpoint_graph(args.right), address=args.address,
            snapshot=args.snapshot, column=args.column, rows=args.rows, edge_ids=(args.left_edges, args.right_edges))
    except (OSError, KeyError, TypeError, ValueError, LightGraphError) as exc:
        parser.exit(1, f"无法核对检查点。{exc}\n")
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
