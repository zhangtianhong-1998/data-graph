"""A taxonomy permission change can depend on a disconnected branch update."""
import pytest

from lightgraph.contracts import RuleSpec
from lightgraph.store import GraphStore
from lightgraph.store.publication_groups import dependency_groups, inspect_group, PublicationGroup
from lightgraph.tests.test_graph_store import edge, complete
from lightgraph.tests.test_publication_groups import stage, node


def stage_tree(store,rid,revision,pairs,allow,policy_location):
    if policy_location=='rule':
        store.put_rule(RuleSpec(rule_id=rid,revision=revision,kind='long_hierarchy',
            source={'source_id':'s','table':rid},identity={'key_columns':['code']},
            hierarchy={'parent_column':'parent','allow_dag':allow,'taxonomy':'shared'}))
    facts=[node(entity) for entity in sorted({v for pair in pairs for v in pair})]
    for a,b in pairs:
        fact=edge(a,b);fact.payload['qualifiers']={'taxonomy':'shared'}
        if policy_location=='qualifier':fact.payload['qualifiers']['allow_dag']=allow
        facts.append(fact)
    return stage(store,rid,facts,revision=revision)


@pytest.mark.parametrize('policy_location',['qualifier','rule'])
@pytest.mark.parametrize('tightening',[False,True])
def test_permission_migration_and_disconnected_parent_update_publish_as_one_unit(policy_location,tightening):
    with GraphStore() as store:
        # Names deliberately make the dependent half sort first, so this would
        # fail if the planner relied on incidental publication order.
        permission='a_permission' if tightening else 'z_permission'
        branch='z_branch' if tightening else 'a_branch'
        permission_old=stage_tree(store,permission,1,[('x','y')],tightening,policy_location)
        old=[permission_old]
        if tightening:
            old.append(stage_tree(store,branch,1,[('a','c'),('b','c')],True,policy_location))
        store.publish_partitions([complete(p) for p in old])
        heads=store.heads()
        permission_new=stage_tree(store,permission,2,[('x','y')],not tightening,policy_location)
        branch_new=stage_tree(store,branch,2 if tightening else 1,
            [('a','c')] if tightening else [('a','c'),('b','c')],True,policy_location)
        # The complete desired graph is valid, but its first sorted half still
        # conflicts with the other half's surviving old head.
        together=PublicationGroup((permission_new,branch_new),(permission,branch))
        assert inspect_group(store,together) is None
        first=permission_new if tightening else branch_new
        assert inspect_group(store,PublicationGroup((first,),(min(permission,branch),))).code=='multiple_parent'
        planned=dependency_groups(store,[permission_new,branch_new])
        assert len(planned)==1 and set(planned[0].rule_ids)=={permission,branch}
        assert store.heads()==heads
        store.publish_partitions([complete(p) for p in planned[0].publication_ids],expected_heads=heads)
        assert {h['publication_id'] for h in store.heads() if h['valid']}=={permission_new,branch_new}
