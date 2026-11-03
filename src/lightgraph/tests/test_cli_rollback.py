"""Retired fixed-rule commands cannot re-enter the former discovery pipeline.

YAML draft history, validation and publication replacement are covered in
 test_graph_yaml_workspace.py and test_construction_runner.py. The previous
 RuleSpec/SQLite rollback command does not operate on this model.
"""
import pytest
from lightgraph.cli import parser


@pytest.mark.parametrize('command', ['rollback', 'demo', 'verify', 'prepare-evaluation', 'run-frozen'])
def test_fixed_rule_cli_entrypoints_are_removed(command):
    with pytest.raises(SystemExit) as exc:
        parser().parse_args([command])
    assert exc.value.code == 2
