"""Resume partial discovery without withdrawing validated independent work."""
from types import SimpleNamespace

from lightgraph.contracts import RuleSpec, SourceRef
from lightgraph.discovery.budget import BudgetAllocator, Cost
from lightgraph.discovery.models import DataUnderstanding, DiscoveryResult
from lightgraph.pipeline import run_pipeline
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import table


def entity(rule_id, name, *, label='name', kind='Metric', domain=''):
    return RuleSpec(rule_id=rule_id, kind='column_group_entity',
        source={'source_id':'s','table':name},
        identity={'key_columns':['code'],'entity_kind':kind,'identity_domain':domain},
        attributes={'single_valued':{'label':label} if label else {}})


def result_for(rules, issues=()):
    return DiscoveryResult(rules=rules, issues=list(issues), understandings=[
        DataUnderstanding(source=r.source, dependency_hash='offline', columns=[], rule_refs=[r.rule_id])
        for r in rules])


def model():
    return SimpleNamespace(budget=BudgetAllocator(Cost(seconds=7200,llm_calls=240,llm_tokens=None)),
        usage={'calls':0}, explore=lambda *a,**kw:None)


def isolate_discovery(monkeypatch, discover, references=None):
    monkeypatch.setattr('lightgraph.discovery.discover',discover)
    monkeypatch.setattr('lightgraph.discovery.coverage.discover_uncovered',lambda **kw:([],{},[]))
    monkeypatch.setattr('lightgraph.discovery.discover_references',references or (
        lambda *a,**kw: DiscoveryResult(relation_table_outcomes={
            name:'resolved' for name in kw['table_refs']})))
