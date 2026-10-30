import json
from lightgraph.evaluation.concepts import evaluate_concepts
from lightgraph.tests.test_concept_mapping import coded_rule,coded_row,execute
from lightgraph.store import GraphStore


def test_exact_joint_contexts_do_not_accept_pairwise_cross_product_or_merge_equal_labels(tmp_path):
    gold={'version':'lightgraph.concepts-gold/1','concepts':[
        {'role':'metric','code':'M01','label':'占用'},{'role':'metric','code':'M02','label':'占用'},
        {'role':'product','code':'M01','label':'梨'},{'role':'channel','code':'C01','label':'批发'}],
        'contexts':[{'subject':'A','metric':'M01','product':'M01','channel':'C01'},
                    {'subject':'B','metric':'M02','product':'M01','channel':'C01'}]}
    goldpath=tmp_path/'gold.json';goldpath.write_text(json.dumps(gold))
    with GraphStore() as store:
        assert execute(store,[coded_row(),coded_row(id='2',subject='B',metric_code='M02')],coded_rule()).published
        graph=store.view({'A','B'})
    path=tmp_path/'graph.json';path.write_text(json.dumps(graph))
    def score():return evaluate_concepts(path,goldpath,role_kinds={'metric':'Metric','product':'Product','channel':'Channel'},
        role_relations={'metric':'binds_metric','product':'binds_product','channel':'binds_channel'},context_kind='ConceptBinding')['accepted']
    result=score()
    assert result['concepts']['exact'] and result['joint_contexts']['exact']
    # Adding a second metric to one context may not multiply it into valid assignments.
    edge=next(e for e in graph['edges'] if e['relation']=='binds_metric')
    other=next(e for e in graph['edges'] if e['relation']=='binds_metric' and e['source']!=edge['source'])
    graph['edges'].append({**other,'source':edge['source']});path.write_text(json.dumps(graph))
    result=score();assert result['invalid_context_nodes']==1 and not result['joint_contexts']['exact']
