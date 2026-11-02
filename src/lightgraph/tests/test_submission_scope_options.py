"""Submission choices keep local join columns separate from remote endpoints."""
from copy import deepcopy

from jsonschema import Draft202012Validator

from lightgraph.discovery.models import ProposalBatch
from lightgraph.discovery.proposal_session import ProposalSession, proposal_tool_specs
from lightgraph.discovery.reference_validation import normalize_column_references
from lightgraph.tests.test_discovery_agent import input_packet


def fixture():
    packet=input_packet();packet['task']='T2_cross_table'
    packet['scope']={'source_id':'s','schema':'public','table':'mapping'}
    packet['authorized_entities']=['left','right']
    packet['catalog']=[{'source_id':'s','schema':'public','name':name,
        'columns':[{'name':'opaque_a'},{'name':'opaque_b'}]} for name in ('mapping','definitions')]
    proposal={'claim':{'source_entity':'left','source_columns':['opaque_a'],
        'target_entity':'right','target_columns':['opaque_b'],'relation_kind':'member_of'},
        'evidence_cited':['c_a']}
    return packet,proposal


def test_same_named_remote_keys_do_not_ambiguous_explicit_local_join_slots():
    packet,proposal=fixture()
    raw=ProposalBatch.model_validate({'task':packet['task'],'proposals':[{'kind':'reference',**proposal}]})
    normalized,count=normalize_column_references(raw,packet)
    assert count==2
    assert normalized.proposals[0].claim.source_columns==['c_a']
    assert normalized.proposals[0].claim.target_columns==['c_b']
    session=ProposalSession(packet)
    assert session.execute('submit_reference',proposal)['status']=='ok'
    assert session.batch().proposals==normalized.proposals


def test_scoped_tool_options_accept_local_names_but_never_remote_ids():
    packet,proposal=fixture()
    schema=next(s['function']['parameters'] for s in proposal_tool_specs(packet['task'],packet)
        if s['function']['name']=='submit_reference')
    validator=Draft202012Validator(schema)
    assert validator.is_valid(proposal)
    foreign=deepcopy(proposal);foreign['claim']['target_columns']=['c_foreign']
    assert not validator.is_valid(foreign)
    session=ProposalSession(packet)
    rejected=session.execute('submit_reference',foreign)
    assert rejected['code']=='unauthorized_column_reference'
    assert rejected['column_scope']=='current_table'
    assert {c['id'] for c in rejected['local_columns']}=={'c_a','c_b'}
    assert not session.drafts
    foreign['claim']['target_columns']=['c_b'];foreign['claim']['target_entity']='unissued'
    assert not validator.is_valid(foreign)


def test_hierarchy_condition_names_are_exposed_before_submission():
    from lightgraph.discovery.hierarchy_plan import hierarchy_plan_tool_schema
    from lightgraph.discovery.field_model_plan import field_model_plan_tool_schema
    for spec in (hierarchy_plan_tool_schema(),field_model_plan_tool_schema()):
        condition=spec['function']['parameters']['properties']['conditions']
        validator=Draft202012Validator(condition)
        assert validator.is_valid({'hierarchy_version':'version_ref'})
        assert not validator.is_valid({'fd_must_pass':'true'})
