from copy import deepcopy
import json
from pathlib import Path
import runpy
import subprocess
import sys

import pytest


audit = runpy.run_path(str(Path(__file__).resolve().parents[3] / "scripts/audit_worksheet_property.py"))[
    "audit_worksheet_property_evidence"]


def test_command_reads_the_committed_checkpoint_markers(tmp_path):
    from lightgraph.construction.checkpoints import commit_checkpoint
    from lightgraph.tests.test_hierarchy_worksheet_rows import _copies

    left, right = _copies()
    for support in right['graph']['edges'][0]['supports']:
        prov = support['provenance']
        prov['columns'].append('owner')
        prov['source_column_ordinals']['owner'] = 6
        prov['property_columns'] = {'department': 'owner'}
        support['properties'] = {'department': ['EBG']}
    markers = []
    for unit in (left, right):
        record = commit_checkpoint(tmp_path, 'run_' + 'b' * 24, unit['contributor_id'], unit,
                                   input_digest='original', graph_kind='node_delta')
        markers.append(str((tmp_path / record['relative_path']).with_name('checkpoint.json')))
    script = Path(__file__).resolve().parents[3] / 'scripts/audit_worksheet_property.py'
    result = subprocess.run([sys.executable, str(script), *markers, '--address', 'archive', 'public', 'catalog',
                             '--snapshot', 'a' * 64, '--column', 'owner', '--rows', '2', '3',
                             '--left-edges', left['graph']['edges'][0]['id'],
                             '--right-edges', right['graph']['edges'][0]['id']], capture_output=True, check=True)
    assert json.loads(result.stdout)['right']['singleton_support_rows_vs_other_graph'] == {'scalar_kind_and_value_match': 2}


def _s(**prov):
    return {"source_row_witness": {"kind": "excel_rows_v1", "snapshot": "a" * 64, "rows": [[10, 11]]},
            "provenance": {"source": {"source_id": "sheet", "schema": "public", "table": "T"},
                           "columns": ["private-column"], "source_column_ordinals": {"private-column": 2}, **prov}}


def _audit(left, right):
    args = {"address": ("sheet", "public", "T"), "snapshot": "a" * 64, "column": "private-column", "rows": [10, 11]}
    before = deepcopy([left, right])
    result = audit({"edges": [{"id": "left", "supports": left}]},
                   {"edges": [{"id": "right", "supports": right}, {"id": "unused", "supports": left}]},
                   edge_ids=(["left"], ["right"]), **args)
    assert before == [left, right]
    assert "private-" not in json.dumps(result)
    return result


def test_aliases_are_read_on_each_support_and_compared_by_row_and_physical_column():
    typed = _s(context_columns=["private-column"])
    typed["row_context"] = {"values": {"private-column": "private-value"}, "value_types": {"private-column": "text"}}
    prop = _s(property_columns={"private-alias": "private-column"})
    good = dict(prop, properties={"private-alias": ["private-value"]})
    wrong_column = deepcopy(good)
    wrong_column["provenance"]["source_column_ordinals"]["private-column"] = 4
    absent = dict(prop, properties={"private-column": ["private-value"]})
    multi = dict(prop, properties={"private-alias": ["private-value", "private-other"]})
    constant = deepcopy(good)
    constant["provenance"]["constant_properties"] = {"private-alias": "private-value"}
    out = _audit([typed], [good, wrong_column, absent, multi, constant])
    assert out["left"]["row_cell"] == {"typed_row_cell": 1}
    assert out["left"]["property_result"] == {"no_mapped_property": 1}
    assert out["right"]["property_result"] == {"single_property_value": 2, "mapped_property_absent": 1,
                                              "multiple_property_values": 1, "also_mapped_as_constant": 1}
    assert out["right"]["singleton_support_rows_vs_other_graph"] == {
        "scalar_kind_and_value_match": 2, "unverified_column_coordinate": 2}
    assert out["right"]["matched_supports"] == 5


def test_audit_keeps_boolean_integer_distinct_and_rejects_non_overlap():
    typed = _s(context_columns=["private-column"])
    typed["source_row_witness"]["rows"] = [[11, 11]]
    typed["row_context"] = {"values": {"private-column": 1}, "value_types": {"private-column": "integer"}}
    prop = _s(property_columns={"alias": "private-column"})
    prop["properties"] = {"alias": [True]}
    out = _audit([typed], [prop])
    assert (out["left"]["requested_rows"], out["left"]["matched_rows"], out["left"]["matched_supports"]) == (2, 1, 1)
    assert out["right"]["singleton_support_rows_vs_other_graph"] == {"no_typed_cell": 1, "different_value": 1}
    typed["source_row_witness"]["rows"] = [[11, 10]]
    with pytest.raises(ValueError, match="row ranges"):
        _audit([typed], [prop])


def test_audit_distinguishes_persisted_string_from_typed_date_and_excludes_multiple_aliases():
    date = "2026-11-22"
    typed = _s(context_columns=["private-column"])
    typed["row_context"] = {"values": {"private-column": date}, "value_types": {"private-column": "date"}}
    prop = _s(property_columns={"alias": "private-column"})
    prop["properties"] = {"alias": [date]}
    duplicate = _s(property_columns={"alias": "private-column", "other": "private-column"})
    duplicate["properties"] = {"alias": [date], "other": [date]}
    empty = dict(prop, properties={"alias": [""]})
    out = _audit([typed], [prop, duplicate, empty])
    assert out["right"]["property_result"] == {
        "single_property_value": 1, "multiple_aliases_same_value": 1, "empty_string_property_value": 1}
    assert out["right"]["singleton_support_rows_vs_other_graph"] == {"serialized_value_matches_type_differs": 2}
    typed["source_row_witness"]["snapshot"] = "b" * 64
    absent = _audit([typed], [prop])["left"]
    assert (absent["requested_rows"], absent["matched_rows"], absent["matched_supports"]) == (2, 0, 0)
