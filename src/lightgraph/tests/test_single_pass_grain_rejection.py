"""Later grain counterevidence withdraws matching drafts without another Agent."""
import pytest

from lightgraph.rules import RuleEngine
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_entity_granularity_review import (
    Reviewer, compile_batch, composite, fixture, hierarchy,
)


@pytest.mark.parametrize('decision', ['refuted', 'unresolved'])
@pytest.mark.parametrize('kind', ['column_group', 'entity_key'])
def test_later_grain_rejection_removes_earlier_identity_and_keeps_hierarchy(decision, kind):
    class LaterRejectionReviewer(Reviewer):
        def complete(self, task, packet, contract):
            if packet['semantic_proposition']['kind'] == 'entity_granularity':
                self.decision = (decision if any(review['kind'] == 'entity_granularity'
                    for review in self.reviews) else 'supported')
            return super().complete(task, packet, contract)

    source, goal, packet = fixture()
    original = composite(packet, kind)
    renamed = composite(packet, kind)
    renamed['claim']['group_id'] = 'renamed_same_identity'
    reviewer = LaterRejectionReviewer()
    rules, result = compile_batch([original, renamed, hierarchy(packet)],
        source, goal, packet, reviewer)

    assert [rule.kind for rule in rules] == ['wide_hierarchy']
    assert [issue['code'] for issue in result.issues] == ['semantic_claim_' + decision]
    assert [review['kind'] for review in reviewer.reviews] == [
        'entity_granularity', 'entity_granularity', 'wide_hierarchy']
    assert reviewer.explorations == 0
    rows = {'public.catalog': [
        ('D', 'A', 'B', 'First', 'X', 'X'), ('D', 'A', 'C', 'Second', 'Y', 'Y')]}
    with MemorySource([source], rows) as adapter, GraphStore() as store:
        assert RuleEngine(store).execute(adapter, rules[0]).published
        graph = store.view({'public'})
        assert len(graph['nodes']) == 3 and len(graph['edges']) == 2
