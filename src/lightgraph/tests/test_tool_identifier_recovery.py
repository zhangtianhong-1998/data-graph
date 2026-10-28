"""Rejected tool references can recover without disclosing their arguments."""
import json

import pytest

from lightgraph.tests.test_discovery_sql_identifiers import source_tools


@pytest.mark.parametrize('name,arguments,code', [
    ('get_metadata', {'table': 'UNKNOWN_TABLE_CANARY'}, 'unauthorized_object'),
    ('check_fd', {'table': 'fruit:public.水果报表项层级', 'keys': ['L1_CODE'],
                  'values': ['UNKNOWN_COLUMN_CANARY']}, 'unauthorized_column'),
])
def test_tool_identifier_errors_point_to_authorized_catalog(source_tools, name, arguments, code):
    source, tools = source_tools
    rejected = tools.execute(name, arguments)
    assert rejected['code'] == code
    assert 'no table argument' in rejected['guidance']
    assert 'tool_table_id' in rejected['guidance']
    assert 'UNKNOWN_TABLE_CANARY' not in json.dumps(rejected)
    assert 'UNKNOWN_COLUMN_CANARY' not in json.dumps(rejected)
    assert source.stats.queries == 0
    metadata = tools.execute('get_metadata', {})['data']['tables'][0]
    accepted = tools.execute('get_metadata', {'table': metadata['tool_table_id']})
    assert accepted['status'] == 'ok'
    assert accepted['data']['tables'][0]['tool_table_id'] == metadata['tool_table_id']
