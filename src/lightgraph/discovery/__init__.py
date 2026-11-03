"""Shared source probes and historical discovery implementation.

The current construction runtime imports only deterministic probe modules. Keep
historical exports lazy so importing those probes cannot load the old model
client, proposal compiler or AgentScope loop into the new runtime.
"""
from importlib import import_module

_EXPORTS = {
    **{name: 'engine' for name in ('discover', 'discover_references', 'DiscoveryEngine',
        'invalidate_dependencies', 'revise_after_counterexample')},
    **{name: 'models' for name in ('DiscoveryResult', 'Hypothesis', 'DataUnderstanding', 'ProposalBatch')},
    **{name: 'budget' for name in ('BudgetAllocator', 'Cost')},
    **{name: 'llm' for name in ('StructuredModelClient', 'ModelConfig', 'validate_proposal_references')},
    **{name: 'cache' for name in ('validate_cached_patterns', 'CachedPatternReview')},
}
__all__ = list(_EXPORTS)


def __getattr__(name):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(name)
    value = getattr(import_module('.' + module, __name__), name)
    globals()[name] = value
    return value
