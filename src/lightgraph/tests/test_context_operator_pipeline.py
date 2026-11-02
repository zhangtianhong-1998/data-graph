"""Competing operators must not duplicate or withdraw an accepted source object."""
import json
import pytest

from lightgraph.contracts import RuleSpec
from lightgraph.pipeline import run_pipeline
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_concept_mapping import coded_rule, coded_row
from lightgraph.tests.test_context_identity import entity_rule
from lightgraph.tests.test_rules_engine import table


@pytest.mark.parametrize('unpublished_revision',[False,True])
def test_new_operator_preserves_prior_node_and_independent_rule_and_retains_candidate(tmp_path,unpublished_revision):
    concept=coded_rule();plain=entity_rule(concept)
    row=coded_row();source_table=table(list(row))
    config=RunConfig(sources=[],output_dir=str(tmp_path),scopes=['public','A'],max_identity_reviews=0)
    with MemorySource([source_table],{'public.items':[row]}) as source:
        first=run_pipeline(config,[source],rules=[plain])
    assert first.rules_published==[plain.rule_id],first.issues
    with GraphStore(tmp_path/'graph.sqlite') as store:
        before={n['id'] for n in store.view({'A'})['nodes'] if n['layer']=='business'}
        revalidated=plain
        if unpublished_revision:
            stored=next(r for r in store.get_rules() if r.rule_id==plain.rule_id)
            revalidated=stored.model_copy(update={'revision':2},deep=True)
            store.put_rule(revalidated)
    independent=RuleSpec(rule_id='unrelated',kind='column_group_entity',source=plain.source,
        scope=plain.scope,identity={'key_columns':['subject','metric_code'],'entity_kind':'Metric'},
        attributes={'single_valued':{'label':'metric'}})
    # The new proposal comes first: accepted rules must not depend on list order.
    with MemorySource([source_table],{'public.items':[row]}) as source:
        report=run_pipeline(config,[source],rules=[concept,revalidated,independent])
    assert report.status=='COMPLETED_WITH_GAPS',report.issues
    assert set(report.rules_published)=={plain.rule_id,independent.rule_id}
    assert concept.rule_id in report.rules_skipped
    assert report.counts['publication_groups_failed']==0
    assert report.counts['competing_context_rules']==1
    with GraphStore(tmp_path/'graph.sqlite') as store:
        graph=store.view({'A'})
        contexts={n['id'] for n in graph['nodes'] if n['kind']=='ConceptBinding'}
        assert contexts==before
        assert len([n for n in graph['nodes'] if n['layer']=='business'])==2
        assert not [e for e in graph['edges'] if e['source'] in contexts]
        candidates=[json.loads(r[0]) for r in store.connection.execute('SELECT payload FROM discovery_candidate')]
        conflict=next(c for c in candidates if c['failure_code']=='conflicting_rule_proposals')
        assert set(conflict['rule_refs'])=={plain.rule_id,concept.rule_id}
        assert len(conflict['mappings'])==2
        assert '占用' not in json.dumps(candidates,ensure_ascii=False)
        assert concept.rule_id not in {r.rule_id for r in store.get_rules()}
