"""A resumed relation task sees prior mappings without conflating endpoint overlap."""
import pytest

from lightgraph.contracts import ExtractionGoal, RuleSpec
from lightgraph.discovery.workflow import discover_references_with_agent
from lightgraph.pipeline import _relation_context_rules
from lightgraph.rules import RuleEngine
from lightgraph.runtime.iteration import IterationLedger
from lightgraph.sources import MemorySource
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import client, input_packet, response
from lightgraph.tests.test_reference_self_link_review import inputs


def definition(name):
    return {'name':name,'description':('The object is assigned to the dimension.' if name=='assigned_to'
        else 'The object reports its results to the dimension.'),
        'source_kinds':['ReportItem'],'target_kinds':['Dimension']}


def existing_relation():
    return RuleSpec(rule_id='existing_assignment',kind='reference',
        source={'source_id':'s','table':'objects'},
        identity={'key_columns':['code'],'entity_kind':'ReportItem'},
        semantic_schema={'relations':[definition('assigned_to')]},
        relation={'source_rule_id':'objects','target_rule_id':'other',
            'source_columns':['code'],'target_columns':['other'],'relation':'assigned_to'},
        evidence={'semantic_basis':['user:explicit']})


@pytest.mark.parametrize('new_meaning',[False,True])
def test_resumed_reference_packet_preserves_mapping_and_vocabulary_but_allows_different_meaning(monkeypatch,new_meaning):
    table,base=inputs();seen=[]
    selected='reports_to' if new_meaning else 'assigned_to'
    def handler(request):
        _,packet=input_packet(request);seen.append(packet['task'])
        if packet['task']=='semantic_review':
            assert packet['semantic_proposition']['relation_definition']['name']==selected
            return response({'proposition_ref':packet['semantic_proposition_ref'],'decision':'supported',
                'basis':'data_inference','evidence':[{'reference':'backend_endpoint_verification','stance':'supports'}]})
        ids={c['name']:c['id'] for c in packet['metadata_digest']['columns']}
        assert set(packet['authorized_entities'])=={'objects','other'}
        assert packet['existing_reference_rules_total']==1
        assert not packet['existing_reference_rules_truncated']
        known=packet['existing_reference_rules'][0]
        assert known['rule_id'] and known['revision']==1
        assert known['relation_contract']['relation']=='assigned_to'
        assert known['relation_contract']['source_columns']==[ids['code']]
        assert known['relation_contract']['target_columns']==[ids['other']]
        assert packet['semantic_schema']['relations'][0]['name']=='assigned_to'
        assert 'Shared endpoints alone do not establish equivalent meaning' in packet['relation_reuse_instruction']
        return response({'task':packet['task'],
            'semantic_schema':{'relations':[definition(selected)]} if new_meaning else {},
            'proposals':[{'kind':'reference','evidence_cited':[ids['code']],
                'claim':{'source_entity':'objects','target_entity':'other',
                    'source_columns':[ids['code']],'target_columns':[ids['other']],'relation_kind':selected}}]})
    with MemorySource([table],{'public.objects':[('A','Alpha','unused','D')]}) as source,GraphStore() as store:
        engine=RuleEngine(store)
        for rule in [*base,existing_relation()]:assert engine.execute(source,rule).published
        rules=store.get_rules()
        endpoints=[rule for rule in rules if rule.kind!='reference']
        result=discover_references_with_agent(source,_relation_context_rules(endpoints,rules,set()),
            model=client(monkeypatch,handler))
        assert len(result.rules)==1 and not result.issues
        assert result.rules[0].relation.relation==selected
        if new_meaning:
            assert engine.execute(source,result.rules[0]).published
            edges=store.view({'public'})['edges']
            assert {edge['relation'] for edge in edges}=={'assigned_to','reports_to'}
            assert len({(edge['source'],edge['target']) for edge in edges})==1
        ledger=IterationLedger(store,ExtractionGoal(),['public'])
        item=ledger.enqueue(existing_relation().source,task='relation_discovery',context_version='v1')
        knowledge=ledger.context([item],rules,iteration=1)['known_rules']
        assert next(rule for rule in knowledge if rule['kind']=='reference')['relation_contract']['relation']=='assigned_to'
        filtered=_relation_context_rules(endpoints,rules,{'existing_assignment'})
        assert all(rule.kind!='reference' for rule in filtered)
    assert seen==['T2_cross_table','semantic_review']


def test_relation_packet_exposes_only_verified_global_identity_domains(monkeypatch):
    from lightgraph.tests.test_shared_label_coverage import fixture
    tables,rules,rows=fixture()
    for rule in rules:
        rule.status='validated';rule.validation_ref='v_'+rule.rule_id
    rules[1].identity.identity_scope='namespace_local'
    def handler(request):
        _,packet=input_packet(request)
        bindings={b['source']['table']:b for b in packet['entity_bindings']}
        assert bindings['occurrences']['shared_identity_domain']=='metric_codes'
        assert bindings['definitions']['shared_identity_domain'] is None
        assert 'sources supporting one object' in packet['relation_reuse_instruction']
        return response({'task':packet['task'],'proposals':[]})
    with MemorySource(tables,rows) as source:
        result=discover_references_with_agent(source,rules,model=client(monkeypatch,handler))
    assert not result.rules and not result.issues
    assert set(result.relation_table_outcomes.values())=={'resolved'}
