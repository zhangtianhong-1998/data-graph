"""Project-owned model limits, including versioned standalone Agent files."""
import yaml
from pydantic import ValidationError

from lightgraph.errors import LightGraphError
from lightgraph.runtime.config import AgentConfig, ModelConfig

LIMIT_FIELDS = frozenset({'max_calls', 'max_prompt_tokens', 'max_output_tokens', 'timeout_seconds',
    'context_window_tokens', 'working_context_tokens'})


def read_model_limits(workspace, pid):
    model = workspace.resolved_config(pid).resolved_model
    return {name: getattr(model, name) for name in sorted(LIMIT_FIELDS)}


def save_model_limits(workspace, pid, patch):
    if not isinstance(patch, dict) or not patch or set(patch) - LIMIT_FIELDS:
        raise LightGraphError('invalid_model_limits')
    config = workspace.get_config(pid)
    resolved = workspace.resolved_config(pid)
    try:
        model = ModelConfig.model_validate({**resolved.resolved_model.model_dump(), **patch})
    except ValidationError as exc:
        from .config_validation import ConfigValidationError
        raise ConfigValidationError(exc, code='invalid_model_limits', prefix=('model',)) from None
    except ValueError:
        raise LightGraphError('invalid_model_limits') from None
    if config.get('agent_config'):
        # Keep the uploaded version intact; saving limits creates a new registered
        # project file, so older run dependency hashes remain meaningful.
        agent = AgentConfig(name=resolved.agent_name, llm=model)
        asset = workspace.add_file(pid, 'agent-settings.yaml',
            yaml.safe_dump(agent.model_dump(mode='json'), allow_unicode=True, sort_keys=False).encode())
        config['agent_config'] = asset['relative_path']
    else:
        config['model'] = model.model_dump(mode='json')
    workspace.save_config(pid, config)
    return read_model_limits(workspace, pid)
