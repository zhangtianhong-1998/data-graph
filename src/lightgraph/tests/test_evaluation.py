import json
import subprocess
import sys
import pytest
from lightgraph.evaluation import evaluate
from lightgraph.evaluation.frozen import prepare, verify, launch, write, load
from lightgraph.errors import LightGraphError


def test_exact_scores_do_not_pool_duplicates_or_candidates(tmp_path):
    gold={'nodes':[{'code':'a','labels':['Alpha'],'english_names':['ALPHA']},{'code':'b'}],
          'edges':[{'source':'a','target':'b'}]}
    graph={'nodes':[{'id':'1','layer':'business','kind':'ReportItem','status':'accepted',
        'properties':{'business_code':'a'},'label':'Alpha','aliases':['ALPHA']},
        {'id':'2','layer':'business','status':'candidate','properties':{'business_code':'b'}}],
        'edges':[{'source':'1','target':'2','relation':'parent_of','status':'candidate'}]}
    write(tmp_path/'gold.json',gold);write(tmp_path/'graph.json',graph)
    write(tmp_path/'report.json',{'status':'COMPLETED_WITH_GAPS'})
    result=evaluate(tmp_path/'graph.json',tmp_path/'gold.json',run_report=tmp_path/'report.json')
    assert result.run_status=='COMPLETED_WITH_GAPS'
    assert result.accepted.identities.recall==.5 and result.accepted.edges.predicted==0
    assert result.accepted.labels.exact and result.accepted.english_names.exact
    assert result.accepted_plus_candidates.identities.exact and result.accepted_plus_candidates.edges.exact
    graph['nodes'].append({**graph['nodes'][0],'id':'split'})
    write(tmp_path/'graph.json',graph)
    result=evaluate(tmp_path/'graph.json',tmp_path/'gold.json')
    assert result.accepted.identities.true_positive==0
    assert result.accepted.duplicates=={'a':['1','split']}
    assert not result.accepted_plus_candidates.edges.exact


def inputs(tmp_path):
    source=tmp_path/'source.csv';source.write_text('code,name\nA,Alpha\n')
    gold=tmp_path/'gold.json';write(gold,{'nodes':[]})
    config=tmp_path/'config.json';write(config,{'sources':[{'id':'s','kind':'csv_file','path':str(source)}],
        'output_dir':str(tmp_path/'unused'),'model':{'mode':'disabled'},'max_identity_reviews':0})
    data=load(config);data['sources'][0].update(encoding='utf-8',delimiter=',',header=True,
        null_values=[''],type_inference='text');write(config,data)
    return source,gold,config




def test_gold_cannot_be_input_even_under_different_name(tmp_path):
    source,gold,config=inputs(tmp_path);source.write_bytes(gold.read_bytes())
    with pytest.raises(LightGraphError,match='gold_in_model_inputs'):prepare(config,tmp_path/'bad',gold_path=gold)




def test_scope_scores_aggregate_verified_supports_and_reject_unknowns(tmp_path):
    from lightgraph.evaluation.core import evaluate_mode
    from lightgraph.evaluation.models import GoldGraph
    gold=GoldGraph(nodes=[{'code':'A','subjects':['one','two']}])
    graph={'nodes':[{'id':'n','status':'accepted','layer':'business'}],
        'facts':[{'id':'exists','fact_kind':'NodeExistence','status':'accepted','payload':{'entity_id':'n'},'support_ids':['s1','s2']},
            {'id':'code','fact_kind':'PropertyValue','status':'accepted','payload':{'entity_id':'n','name':'business_code','value':'A'}}],
        'supports':[{'id':'s1','status':'accepted','scope_ref':'one'},{'id':'s2','status':'accepted','scope_ref':'two'}]}
    assert evaluate_mode(graph,gold,{'accepted'},{}).subject_verified_identities.exact
    graph['supports'][1]['scope_ref']='public'
    assert evaluate_mode(graph,gold,{'accepted'},{}).unverified_scope_node_ids==['n']
    graph['nodes'].append(dict(graph['nodes'][0]))
    result=evaluate_mode(graph,gold,{'accepted'}, {})
    assert result.duplicate_node_ids==['n'] and result.identities.predicted==2 and result.identities.true_positive==0
