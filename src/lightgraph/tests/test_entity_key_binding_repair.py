"""Known but unbound attributes must not discard independent hierarchy rules."""
import pytest

from lightgraph.contracts import ExtractionGoal
from lightgraph.discovery import discover
from lightgraph.discovery.engine import _column_id, _compile_model
from lightgraph.discovery.models import ProposalBatch
from lightgraph.discovery.reference_validation import validate_proposal_references
from lightgraph.errors import LightGraphError
from lightgraph.sources import MemorySource
from lightgraph.rules import RuleEngine
from lightgraph.store import GraphStore
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


@pytest.mark.parametrize('binding',['unbound','valid','excluded_attribute'])
def test_attribute_binding_and_exclusion_are_checked_once_with_valid_hierarchy_preserved(monkeypatch,binding):
    t=table('objects',['a','b','c','d','e','f','g']);tasks=[]
    def handler(request):
        _,packet=input_packet(request);tasks.append(packet['task'])
        if packet['task']=='semantic_review':
            return response({'proposition_ref':packet['semantic_proposition_ref'],
                'decision':'supported','basis':'data_inference','evidence':[{
                    'reference':packet['metadata_digest']['columns'][2]['id'],'stance':'supports'}]})
        assert packet['task']=='T0_discovery'
        proposals=[entity(packet,fixed=binding!='unbound',excluded_attribute=binding=='excluded_attribute'),
            hierarchy(packet,{}),{'kind':'exclusion',
                'claim':{'exclusions':[{'column_id':'f','reason':'out_of_goal',
                    'affects':['identity','projection']}]},
                'evidence_cited':[packet['metadata_digest']['columns'][5]['id']]}]
        return response({'task':packet['task'],'proposals':proposals})
    model=client(monkeypatch,handler)
    with MemorySource([t],{'public.objects':[('A','Alpha','P','C','Alias','Hidden','Text')]}) as source,GraphStore() as store:
        result=discover(source,model=model)
        assert [rule.kind for rule in result.rules].count('wide_hierarchy')==1,result.issues
        for rule in result.rules:
            assert RuleEngine(store).execute(source,rule).published
            assert 'f' not in rule.identity.key_columns
            assert 'f' not in rule.attributes.single_valued.values()
            if rule.kind=='column_group_entity':
                assert 'f' in {excluded.column for excluded in rule.attributes.excluded}
        graph=store.view({'public'})
        assert len(graph['nodes'])==(3 if binding=='valid' else 2)
        assert len(graph['edges'])==1 and graph['edges'][0]['relation']=='parent_of'
        assert all('Hidden' not in node['properties'].values() for node in graph['nodes'])
    assert tasks==['T0_discovery','semantic_review']
    assert 'reference_repair_attempts' not in model.usage
    if binding=='valid':assert not result.issues
    else:
        expected='unbound_attribute_property' if binding=='unbound' else 'excluded_column_projected'
        assert any(issue['code']==expected for issue in result.issues)
        if binding=='unbound':
            assert any(issue['code']==expected and issue['status']=='deferred' for issue in result.issues)
