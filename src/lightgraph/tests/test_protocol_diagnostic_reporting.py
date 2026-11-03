"""Protocol reporting identifies failing stages without exposing model data."""
import json

from lightgraph.contracts import RunReport, canonical
from lightgraph.pipeline import _discovery_outcome, _model_protocol_issues
from lightgraph.web.execution import _safe_report


def test_protocol_stage_survives_report_and_web_without_private_values():
    private = 'PRIVATE_MODEL_VALUE_578'
    histogram = {canonical({'stage': 'provider_usage', 'field': ['usage', 'prompt_tokens'],
        'code': 'invalid_usage'}): 2,
        canonical({'stage': 'response_schema', 'field': [private], 'code': 'missing',
            'input': private}): 1}
    issues = _model_protocol_issues({'model_protocol_diagnostics': histogram})
    report = RunReport(run_id='diagnostic-test', issues=issues)
    visible = _safe_report(report.model_dump(mode='json'))
    assert visible['issues'][0] == {'code': 'model_protocol_diagnostic', 'status': 'diagnostic',
        'stage': 'provider_usage', 'field_path': '["usage","prompt_tokens"]',
        'constraint': 'invalid_usage', 'count': 2}
    assert private not in canonical(visible)
    assert json.loads(visible['issues'][1]['field_path']) == ['*']
    assert _discovery_outcome(issues) == 'COMPLETED'
    assert _discovery_outcome(issues + [{'code': 'llm_schema_invalid', 'status': 'deferred'}]) == 'PARTIAL'


def test_protocol_report_rejects_untrusted_stage_constraint_and_malformed_histogram():
    private = 'PRIVATE_MODEL_VALUE_578'
    assert _model_protocol_issues({'model_protocol_diagnostics': []}) == []
    assert _model_protocol_issues({'model_protocol_diagnostics': {
        private: 1, canonical({'stage': private, 'field': [], 'code': 'missing'}): 1,
        canonical({'stage': 'response_schema', 'field': [], 'code': private}): 1,
        canonical({'stage': 'response_schema', 'field': [], 'code': 'missing'}): True}}) == []
    report = RunReport(run_id='invalid-diagnostic', issues=[{'code': 'model_protocol_diagnostic',
        'stage': private, 'constraint': private, 'field_path': canonical([private]), 'input': private}])
    visible = _safe_report(report.model_dump(mode='json'))
    assert private not in canonical(visible)
    assert 'stage' not in visible['issues'][0] and 'constraint' not in visible['issues'][0]
