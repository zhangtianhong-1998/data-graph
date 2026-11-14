"""Actionable configuration diagnostics without values, secrets or raw errors."""
from pydantic import ValidationError

from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import RunConfig, ModelConfig, SourceConfig, AgentConfig, GraphScopeBinding, InheritedGraphRef, WorkflowConfig, WorkflowUnitConfig, MergePolicyConfig
from lightgraph.contracts import QueryLimits, ExtractionGoal

_FIELDS = frozenset().union(*(model.model_fields for model in (
    RunConfig, ModelConfig, SourceConfig, AgentConfig, GraphScopeBinding,
    InheritedGraphRef, WorkflowConfig, WorkflowUnitConfig, MergePolicyConfig,
    QueryLimits, ExtractionGoal)))
_HINTS = {
    'missing': '缺少必填字段。',
    'extra_forbidden': '存在不支持的字段，请检查字段名。',
    'int_type': '请填写整数，不要留空。',
    'int_parsing': '请填写整数。',
    'int_from_float': '请填写整数。',
    'float_type': '请填写数字，不要留空。',
    'float_parsing': '请填写数字。',
    'greater_than': '数值须大于允许的下限。',
    'greater_than_equal': '数值低于允许的下限。',
    'less_than_equal': '数值超过允许的上限。',
    'literal_error': '选项不受支持，请检查配置字段的可选值。',
    'string_type': '请填写文本。',
    'list_type': '请填写 JSON 数组。',
    'dict_type': '请填写 JSON 对象。',
    'model_type': '请填写 JSON 对象。',
}
_RULES = {
    'context_window_must_exceed_output_reservation': '总上下文窗口须大于最大输出 tokens。',
    'invalid_model_key_environment': '环境变量名只能包含英文字母、数字和下划线，且不能以数字开头。',
    'csv_format_config_required': 'CSV 来源需要路径、编码、分隔符、表头、空值和类型设置；建议从数据接入页面添加。',
    'source_id_required': '来源编号不能为空。',
}


class ConfigValidationError(LightGraphError):
    def __init__(self, error: ValidationError, *, code='invalid_project_config', prefix=()):
        super().__init__(code)
        self.diagnostics = []
        for item in error.errors(include_input=False, include_url=False)[:16]:
            parts = []
            for part in (*prefix, *item['loc']):
                if isinstance(part, int):
                    parts.append(f'[{part}]')
                else:
                    # Unknown dict keys can themselves contain submitted secrets.
                    parts.append(('.' if parts else '') + (part if part in _FIELDS else '<field>'))
            rule = str(item.get('ctx', {}).get('error', ''))
            self.diagnostics.append({'path': ''.join(parts) or 'config', 'type': item['type'],
                'hint': _RULES.get(rule, _HINTS.get(item['type'], '字段格式或字段组合不符合要求，请检查此位置。'))})
