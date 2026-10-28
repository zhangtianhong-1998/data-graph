"""Known but unbound attributes must not discard independent hierarchy rules."""
import pytest

from lightgraph.contracts import ExtractionGoal
from lightgraph.discovery import discover
from lightgraph.discovery.engine import _column_id, _compile_model
from lightgraph.discovery.models import ProposalBatch
from lightgraph.discovery.reference_validation import validate_proposal_references
from lightgraph.errors import LightGraphError
from lightgraph.sources import MemorySource
from lightgraph.tests.test_agent_pipeline import client, input_packet, response, table
from lightgraph.tests.test_compiled_proposal_contract import hierarchy


def entity(packet, *, fixed=False, excluded_attribute=False):
    # Names are exact issued metadata aliases, not test-specific ID workarounds.
    mapping = {'b': 'label', 'e': 'alias'}
    single = ['b']
    if not fixed:
        mapping.update({'a': 'business_code', 'f': 'description', 'g': 'definition'})
    if excluded_attribute:
        single.append('f'); mapping['f'] = 'description'
    return {'kind': 'entity_key', 'claim': {'group_id': 'entity',
        'entity_kind_hint': 'ReportItem', 'key_columns': ['a'],
        'attributes': {'single_valued': single, 'multi_valued': ['e'], 'property_names': mapping}},
        'evidence_cited': [packet['metadata_digest']['columns'][0]['id']]}


def test_issued_unbound_mapping_is_authorized_but_not_compilable():
    t = table('objects', ['a', 'b', 'c', 'd', 'e', 'f', 'g'])
    ids = {c.name: _column_id(t, c.name) for c in t.columns}
    packet = {'task': 'T0_discovery', 'metadata_digest': {'columns': [
        {'name': name, 'id': identifier} for name, identifier in ids.items()]}}
    proposal = entity(packet)
    proposal['claim']['key_columns'] = [ids['a']]
    attrs = proposal['claim']['attributes']
    attrs['single_valued'] = [ids['b']]; attrs['multi_valued'] = [ids['e']]
    attrs['property_names'] = {ids[name]: prop for name, prop in attrs['property_names'].items()}
    batch = ProposalBatch.model_validate({'task': packet['task'], 'proposals': [proposal]})
    validate_proposal_references(batch, packet)
    with pytest.raises(LightGraphError, match='unbound_attribute_property'):
        _compile_model(batch, t, ExtractionGoal(), [], [])


@pytest.mark.parametrize('repair_outcome', ['fixed', 'unchanged', 'excluded_attribute'])
def test_bad_attribute_binding_keeps_valid_hierarchy_and_global_exclusion(monkeypatch, repair_outcome):
    t = table('objects', ['a', 'b', 'c', 'd', 'e', 'f', 'g'])
    tasks = []
    def handler(request):
        _, packet = input_packet(request); tasks.append(packet['task'])
        if packet['task'] == 'semantic_review':
            return response({'proposition_ref': packet['semantic_proposition_ref'],
                'decision': 'supported', 'basis': 'data_inference', 'evidence': [{
                    'reference': packet['metadata_digest']['columns'][2]['id'], 'stance': 'supports'}]})
        if packet['task'] == 'T0_discovery':
            proposals = [entity(packet), hierarchy(packet, {}), {'kind': 'exclusion',
                'claim': {'exclusions': [{'column_id': 'f', 'reason': 'out_of_goal',
                    'affects': ['identity', 'projection']}]},
                'evidence_cited': [packet['metadata_digest']['columns'][5]['id']]}]
        else:
            assert packet['task'] == 'T5_revision'
            assert [failure['code'] for failure in packet['proposal_repairs']] == ['unbound_attribute_property']
            proposals = [entity(packet, fixed=repair_outcome != 'unchanged',
                               excluded_attribute=repair_outcome == 'excluded_attribute')]
        return response({'task': packet['task'], 'proposals': proposals})
    model = client(monkeypatch, handler)
    with MemorySource([t], {'public.objects': [('A', 'Alpha', 'P', 'C', 'Alias', 'Hidden', 'Text')]}) as source:
        result = discover(source, model=model)
    assert tasks == ['T0_discovery', 'semantic_review', 'T5_revision']
    assert 'reference_repair_attempts' not in model.usage
    assert result.stats['compile_repair_rounds'] == 1
    assert [rule.kind for rule in result.rules].count('wide_hierarchy') == 1, result.issues
    if repair_outcome == 'fixed':
        assert {rule.kind for rule in result.rules} == {'wide_hierarchy', 'column_group_entity'}
        assert result.stats['recovered_proposals'] == 1 and not result.issues
    else:
        assert len(result.rules) == 1
        expected = 'unbound_attribute_property' if repair_outcome == 'unchanged' else 'excluded_column_projected'
        assert any(issue['code'] == expected for issue in result.issues)
    for rule in result.rules:
        if rule.kind == 'column_group_entity':
            assert 'f' in {excluded.column for excluded in rule.attributes.excluded}
        assert 'f' not in rule.identity.key_columns
        assert 'f' not in rule.attributes.single_valued.values()
