"""Semantic interpretation precedes full execution without waiving either gate."""
from types import SimpleNamespace

import pytest

from lightgraph.discovery.semantics import review_semantics, rule_evidence
from lightgraph.errors import LightGraphError
from lightgraph.store import GraphStore
from lightgraph.tests.test_agent_pipeline import client, input_packet, response
from lightgraph.tests.test_relative_hierarchy import execute, path, rule


CANARY='EPHEMERAL_CONDITION_REASON_64321'


def proposal(kind='wide_hierarchy'):
    return {'kind':kind,'claim':{'path_semantics':'relative_path','namespace_columns':['c_namespace']}}


def review_response(packet,state='satisfied'):
    return {'proposition_ref':packet['semantic_proposition_ref'],'decision':'supported',
        'basis':'data_inference','evidence':[{'reference':'probe_1','stance':'supports'}],
        'conditions':[{'state':state,'evidence_refs':['probe_1'],'explanation':CANARY}]}


def test_supported_condition_explanation_is_ephemeral_and_execution_remains_pending(monkeypatch,capsys,caplog,tmp_path):
    calls=[]
    def handler(request):
        _,packet=input_packet(request);calls.append(packet['task'])
        stage=packet['review_stage_contract']
        assert stage['stage']=='semantic_interpretation_before_rule_execution'
        assert stage['supported_decision_allows']=='draft_rule_compilation_only'
        assert stage['publication_authorized'] is False
        checks=stage['programmatic_checks']
        assert checks['state']=='pending_if_rule_compiles'
        assert 'identity_namespace_nulls' in checks['checks']
        assert 'hierarchy_self_loop_cycle_and_parent_policy' in checks['checks']
        assert 'single_valued_attribute_fd' not in checks['checks']
        assert 'unknown or unsatisfied business conditions still block' in packet['subtask']
        return response(review_response(packet))
    model=client(monkeypatch,handler)
    refs=review_semantics(model,{'probe_digest':[{'probe_id':'probe_1'}]},proposal(),[])
    evidence=rule_evidence(refs,reviewed=True)
    assert calls==['semantic_review']
    assert evidence.semantic_status=='supported' and evidence.conditions_satisfied
    assert CANARY not in str(refs)+evidence.model_dump_json()+str(model.usage)
    spec=rule();spec.evidence=evidence
    with GraphStore() as store:
        assert execute(store,spec,[path('A','B')]).published
        assert CANARY not in store.get_rules()[0].model_dump_json()
    assert CANARY not in capsys.readouterr().out+caplog.text
    assert list(tmp_path.iterdir())==[]


@pytest.mark.parametrize('state',['unknown','unsatisfied'])
def test_business_condition_explanation_does_not_weaken_terminal_gate(monkeypatch,state,capsys,caplog):
    calls=[]
    def handler(request):
        _,packet=input_packet(request);calls.append(packet['task'])
        return response(review_response(packet,state))
    model=client(monkeypatch,handler)
    with pytest.raises(LightGraphError,match='semantic_condition_unresolved'):
        review_semantics(model,{'probe_digest':[{'probe_id':'probe_1'}]},proposal(),[])
    assert calls==['semantic_review']
    assert CANARY not in capsys.readouterr().out+caplog.text+str(model.usage)


@pytest.mark.parametrize('kind,expected,absent',[
    ('column_group','single_valued_attribute_fd','prospective_graph_topology'),
    ('entity_key','identity_key_nulls','reference_endpoint_resolution'),
    ('long_hierarchy','hierarchy_parent_reference_resolution','identity_namespace_nulls'),
    ('reference','reference_endpoint_resolution','single_valued_attribute_fd'),
    ('entity_granularity','single_valued_attribute_fd','prospective_graph_topology'),
])
def test_stage_describes_only_checks_applicable_to_proposal_kind(kind,expected,absent):
    value=proposal(kind)
    if kind=='entity_granularity':value['candidate']={'kind':'entity_key','claim':{}}
    def complete(task,packet,schema):
        checks=packet['review_stage_contract']['programmatic_checks']['checks']
        assert expected in checks and absent not in checks
        return schema.model_validate(review_response(packet))
    assert review_semantics(SimpleNamespace(complete=complete),
        {'probe_digest':[{'probe_id':'probe_1'}]},value,[])


def test_supported_semantics_does_not_publish_a_cycle_or_null_identity_namespace():
    model=SimpleNamespace(complete=lambda task,packet,schema:schema.model_validate(review_response(packet)))
    refs=review_semantics(model,{'probe_digest':[{'probe_id':'probe_1'}]},proposal(),[])
    for rows,expected in [([path('A','B'),path('B','A')],'hierarchy_cycle'),
                          ([path('A','B',tenant=None)],'null_identity_namespace')]:
        spec=rule();spec.evidence=rule_evidence(refs,reviewed=True)
        with GraphStore() as store:
            outcome=execute(store,spec,rows)
            assert not outcome.published and expected in outcome.codes
            assert not store.view({'t'})['nodes']
