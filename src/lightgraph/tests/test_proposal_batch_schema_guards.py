"""Model-visible task/abstention constraints agree with executable validation."""
import json
from pathlib import Path
from typing import get_args

from jsonschema import Draft202012Validator
from pydantic import ValidationError
import pytest

from lightgraph.discovery.models import ProposalBatch, Task, TASK_PROPOSAL_KINDS


PROPOSALS={
    'column_group':{'group_id':'g','entity_kind_hint':'Metric','members':[
        {'column_id':'c1','role':'identity_code','in_identity':True}]},
    'entity_key':{'group_id':'g','entity_kind_hint':'Metric','key_columns':['c1'],'attributes':{}},
    'wide_hierarchy':{'group_id':'g','levels':[{'position':1,'key':'c1'},{'position':2,'key':'c2'}]},
    'long_hierarchy':{'group_id':'g','key_column':'c1','parent_column':'c2'},
    'reference':{'source_columns':['c1'],'target_entity':'target','target_columns':['c2']},
    'discriminator':{'discriminating_column':'c1','partitions':[{
        'selector_ref':'selector','applies_to_entity':'entity','source_value_column':'c1'}]},
    'exclusion':{'exclusions':[{'column_id':'c1','reason':'out_of_goal','affects':['identity']}]},
}


def runtime_accepts(value):
    try:
        ProposalBatch.model_validate(value)
    except ValidationError:
        return False
    return True


@pytest.mark.parametrize('task',get_args(Task))
@pytest.mark.parametrize('kind',PROPOSALS)
@pytest.mark.parametrize('abstain',[None,False,True])
def test_task_kind_and_abstention_acceptance_match_runtime(task,kind,abstain):
    value={'task':task,'proposals':[{'kind':kind,'claim':PROPOSALS[kind],'evidence_cited':['c1']}]}
    if abstain is not None:value['abstain']=abstain
    expected=kind in TASK_PROPOSAL_KINDS[task] and abstain is not True
    assert runtime_accepts(value) is expected
    assert Draft202012Validator(ProposalBatch.model_json_schema()).is_valid(value) is expected


@pytest.mark.parametrize('task',get_args(Task))
@pytest.mark.parametrize('abstain',[None,False,True])
@pytest.mark.parametrize('proposals',[None,[]])
def test_empty_or_omitted_proposals_remain_valid(task,abstain,proposals):
    value={'task':task}
    if proposals is not None:value['proposals']=proposals
    if abstain is not None:value['abstain']=abstain
    assert runtime_accepts(value)
    assert Draft202012Validator(ProposalBatch.model_json_schema()).is_valid(value)


def test_persisted_schema_has_shared_guards_and_preserves_closed_fields():
    schema=ProposalBatch.model_json_schema()
    Draft202012Validator.check_schema(schema)
    assert set(TASK_PROPOSAL_KINDS)==set(get_args(Task))
    assert set(PROPOSALS)==set(schema['properties']['proposals']['items']['discriminator']['mapping'])
    persisted=Path(__file__).resolve().parents[1]/'schemas/ProposalBatch.schema.json'
    assert json.loads(persisted.read_text())==schema
    value={'task':'T3_hierarchy','proposals':[{'kind':'wide_hierarchy',
        'claim':{**PROPOSALS['wide_hierarchy'],'order_evidence':['c1']},'evidence_cited':['c1']}]}
    assert not runtime_accepts(value)
    assert not Draft202012Validator(schema).is_valid(value)
