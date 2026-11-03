"""Short captions do not weaken identity, scope, or current-name checks."""
import json

import pytest

from lightgraph.evaluation.concepts import evaluate_concepts
from lightgraph.store import GraphStore
from lightgraph.tests.test_concept_mapping import coded_row, coded_rule, execute


def fixture(tmp_path):
    gold={'version':'lightgraph.concepts-gold/1','concepts':[
        {'role':'metric','code':'M01','label':'占用'},
        {'role':'product','code':'M01','label':'梨'},
        {'role':'channel','code':'C01','label':'批发'}],
        'contexts':[{'subject':subject,'metric':'M01','product':'M01','channel':'C01'} for subject in ('A','B')]}
    goldpath=tmp_path/'gold.json';goldpath.write_text(json.dumps(gold),encoding='utf-8')
    with GraphStore() as store:
        assert execute(store,[coded_row(),coded_row(subject='B')],coded_rule()).published
        graph=store.view({'A','B'})
    path=tmp_path/'graph.json'
    def score():
        path.write_text(json.dumps(graph),encoding='utf-8')
        return evaluate_concepts(path,goldpath,role_kinds={'metric':'Metric','product':'Product','channel':'Channel'},
            role_relations={role:'binds_'+role for role in ('metric','product','channel')},
            context_kind='ConceptBinding')['accepted']
    return graph,score


def test_distinct_scoped_contexts_can_share_a_short_caption(tmp_path):
    graph,score=fixture(tmp_path)
    result=score()
    assert result['joint_contexts']['exact'] and result['labels']['exact']
    assert result['context_presentation']['passed']
    assert len(result['context_presentation']['duplicate_label_groups'])==1
    assert not result['context_presentation']['duplicate_code_groups']
    assert {tuple(n['scopes']) for n in graph['nodes'] if n['kind']=='ConceptBinding'}=={('A',),('B',)}


@pytest.mark.parametrize('damage',['scope','missing_scope','component_name','generic_caption','duplicate_code'])
def test_short_caption_cannot_hide_invalid_evidence_or_codes(tmp_path,damage):
    graph,score=fixture(tmp_path)
    contexts=[n for n in graph['nodes'] if n['kind']=='ConceptBinding']
    if damage=='scope':contexts[0]['scopes']=['invented_scope']
    elif damage=='missing_scope':contexts[0].pop('scopes')
    elif damage=='component_name':
        metric=next(n for n in graph['nodes'] if n['kind']=='Metric')
        for fact in graph['facts']:
            if (fact['fact_kind']=='PropertyValue' and fact['payload']['entity_id']==metric['id']
                    and fact['payload']['name']=='label'):
                fact['payload']['value']='篡改后的名称'
    elif damage=='generic_caption':contexts[0]['display_label']='组合上下文'
    else:
        ids={n['id'] for n in contexts}
        for fact in graph['facts']:
            if (fact['fact_kind']=='PropertyValue' and fact['payload']['entity_id'] in ids
                    and fact['payload']['name']=='business_code'):
                fact['payload']['value']='same_context_code'
    result=score()
    assert result['joint_contexts']['exact']  # Structural score stays independent.
    assert not result['context_presentation']['passed']
    if damage=='component_name':assert not result['labels']['exact']
    if damage=='duplicate_code':assert len(result['context_presentation']['duplicate_code_groups'])==1
