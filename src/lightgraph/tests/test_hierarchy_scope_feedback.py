"""A scoped single-table hierarchy must not accidentally request shared identity."""
import asyncio
from copy import deepcopy
import json

import pytest
from pydantic import ValidationError

from lightgraph.discovery.agentscope_tools import create_toolkit
from lightgraph.discovery.field_model_plan import FieldModelHierarchyPlan, field_model_plan_tool_schema
from lightgraph.discovery.hierarchy_plan import HierarchyPlan, hierarchy_plan_tool_schema
from lightgraph.discovery.proposal_session import ProposalSession, proposal_tool_specs
from lightgraph.tests.test_agentscope_tools import call
from lightgraph.tests.test_discovery_agent import input_packet
from lightgraph.tests.test_hierarchy_plan import fixture
from lightgraph.tests.test_proposal_session import group


def retained_fixture():
    _, packet, plan = fixture()
    positions = deepcopy(plan['positions'])
    for position in positions:
        position['aliases'] = [position.pop('alias')]
    packet['schema_knowledge'] = {'objects': [{
        'object_id': 'retained', 'status': 'candidate', 'positions': positions,
        'namespace_columns': list(plan['namespace_columns']),
    }]}
    reduced = {key: value for key, value in plan.items()
               if key not in {'positions', 'namespace_columns'}}
    return packet, plan, {'object_id': 'retained', **reduced}


@pytest.mark.parametrize('operator,index', [
    ('submit_hierarchy_plan', 1), ('submit_field_model_hierarchy', 2),
])
@pytest.mark.parametrize('scope', ['namespace', 'global'])
@pytest.mark.parametrize('domain', [None, '   '])
def test_missing_shared_domain_has_field_path_and_safe_repair_choices(operator, index, scope, domain):
    values = retained_fixture()
    plan = deepcopy(values[index])
    plan.update(identity_scope=scope, plan_id='PRIVATE_SOURCE_VALUE_7621')
    if domain is not None:
        plan['shared_identity_domain'] = domain
    original = deepcopy(plan)
    session = ProposalSession(values[0])
    rejected = session.execute(operator, plan)
    assert rejected['code'] == 'invalid_proposal_arguments'
    diagnostic = rejected['diagnostics'][0]
    assert diagnostic['path'] == ['shared_identity_domain']
    assert diagnostic['constraint'] == 'hierarchy_plan_shared_domain_required'
    assert 'identity_scope=source' in diagnostic['instruction']
    assert 'preserves namespace_columns' in diagnostic['instruction']
    assert 'PRIVATE_SOURCE_VALUE_7621' not in json.dumps(rejected)
    assert plan == original and not session.drafts

    # The Agent makes the choice; the repair does not infer a shared domain.
    plan.update(identity_scope='source')
    plan.pop('shared_identity_domain', None)
    accepted = session.execute(operator, plan)
    assert accepted['status'] == 'ok' and accepted['confirmed'] is False
    claim = session.drafts[0].claim
    assert claim.namespace_columns == values[1]['namespace_columns']
    assert claim.identity_domain.startswith('plan_source:')
    assert claim.identity_scope == 'namespace_local'


def test_source_scope_rejects_supplied_shared_domain_without_rewriting_it():
    packet, _, plan = retained_fixture()
    plan['shared_identity_domain'] = 'PRIVATE_DOMAIN_7621'
    session = ProposalSession(packet)
    result = session.execute('submit_field_model_hierarchy', plan)
    assert result['diagnostics'][0]['path'] == ['shared_identity_domain']
    assert result['diagnostics'][0]['constraint'] == 'hierarchy_plan_unrequested_shared_domain'
    assert 'PRIVATE_DOMAIN_7621' not in json.dumps(result)
    assert plan['shared_identity_domain'] == 'PRIVATE_DOMAIN_7621' and not session.drafts


@pytest.mark.parametrize('contract,index', [(HierarchyPlan, 1), (FieldModelHierarchyPlan, 2)])
@pytest.mark.parametrize('scope', ['namespace', 'global'])
def test_identity_scope_assignment_rechecks_shared_domain(contract, index, scope):
    plan = contract.model_validate(retained_fixture()[index])
    with pytest.raises(ValidationError, match='hierarchy_plan_shared_domain_required'):
        plan.identity_scope = scope

    # The inverse change must not silently retain a formerly shared domain.
    shared = contract.model_validate({**retained_fixture()[index],
        'identity_scope': scope, 'shared_identity_domain': 'approved_domain'})
    with pytest.raises(ValidationError, match='hierarchy_plan_unrequested_shared_domain'):
        shared.identity_scope = 'source'


def test_both_tools_explain_local_namespaces_and_explicit_sharing_before_submission():
    direct = hierarchy_plan_tool_schema()['function']['parameters']['properties']
    retained = field_model_plan_tool_schema()['function']['parameters']['properties']
    for field in ('identity_scope', 'shared_identity_domain', 'position_meaning',
                  'allow_multiple_parents', 'repeat_is_fill', 'relation_type', 'evidence'):
        assert retained[field]['description'] == direct[field]['description']
    assert 'namespace_columns' in retained['identity_scope']['description']
    assert 'Do not invent' in retained['shared_identity_domain']['description']


def test_string_claim_feedback_names_expected_object_and_preserves_existing_draft():
    class SubmissionTools:
        def tool_specs(self):
            return proposal_tool_specs('T1_table_discovery')

    session = ProposalSession(input_packet())
    toolkit = create_toolkit(SubmissionTools(), session.execute)
    assert asyncio.run(call(toolkit, 'submit_column_group', group()))['status'] == 'ok'
    malformed = group()
    malformed['claim'] = json.dumps({'PRIVATE_FIELD_7621': 'PRIVATE_VALUE_7621'})
    rejected = asyncio.run(call(toolkit, 'submit_column_group', malformed))
    assert rejected['code'] == 'invalid_tool_arguments'
    assert rejected['diagnostics'] == [{
        'path': ['claim'], 'constraint': 'type', 'expected_type': 'object',
    }]
    assert 'PRIVATE_' not in json.dumps(rejected)
    assert len(session.drafts) == 1
    assert session.execute('finish_discovery', {'pending': False})['status'] == 'ok'
    assert len(session.batch().proposals) == 1
