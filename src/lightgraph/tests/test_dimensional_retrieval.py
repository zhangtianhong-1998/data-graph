"""Exercise the public query contract against a real scoped SQLite retrieval index."""
import pytest

from lightgraph.retrieval import SubgraphRetriever, sync_graph_to_retrieval
from lightgraph.retrieval.query import RetrieveSubgraphRequest


def graph():
    entities = [
        ('fact', '观察', '本月观察', 'F1'), ('rev', '指标', '销售收入', '001'), ('east', '地区', '华东', 'E'),
        ('router', '产品', '路由器', 'R'), ('r1', '型号', '销售型号甲', 'R1'), ('r2', '型号', '其他型号', 'R2'),
        ('west', '地区', '华西', 'W'), ('k', '指标', '保密指标', 'K1'),
        ('r-alt', '产品', '路由器', 'RR'), ('remote', '产品', '保密产品', 'RK')]
    values = {'fact_code': 'F1', 'metric': '001', 'region': 'E', 'product': 'R', 'model': 'R1', 'other': 'R2'}

    def support(label, *, project='M', row='c1', src='fact_code', dst='metric'):
        return {'scope_ref':'public','label':label, 'properties':{},
                'business_context':{'version':1,'status':'scoped','dimensions':{'project':project}},
                'provenance':{'source':{'source_id':'db','schema':'public','table':'income'},
                              'context_columns':sorted(values),'evidence_refs':['e_' + row],
                              'source_key_columns':[src], 'target_key_columns':[dst]},
                'row_context':{'id':row,'values':values.copy()}}

    nodes = []
    for nid, typ, label, code in entities:
        obs = support(label, project='K' if nid in {'k','remote'} else 'M')
        obs.pop('row_context')
        nodes.append({'id':nid,'type':typ,'identity':{'code':code},'supports':[obs]})
    edges = []
    for a, rel, b, src, dst, row in [
        ('fact','observes','rev','fact_code','metric','c1'), ('fact','in_region','east','fact_code','region','c1'),
        ('router','product_of','fact','product','fact_code','c1'), ('router','has_child','r1','product','model','c1'),
        ('router','has_child','r2','product','other','c2')]:
        edges.append({'id':a+'-'+b,'source':a,'target':b,'type':rel,'supports':[support('',row=row,src=src,dst=dst)]})
    return {'support_visibility_version':1,'definitions':{'entities':[],'relations':[]},'nodes':nodes,'edges':edges}


@pytest.fixture
def run(tmp_path):
    def query(text=None, *, data=None, fixture=None):
        sync_graph_to_retrieval(fixture or graph(), tmp_path)
        req = RetrieveSubgraphRequest.model_validate({'query':text} if data is None else data)
        return req.run(SubgraphRetriever(tmp_path), ['public'])
    return query


def test_long_query_joins_all_roles_from_one_source_tuple_before_drilldown(run):
    query='背景：上次讨论了 K 项目的保密指标。现在只看 M 项目，查询华东的路由器销售收入，按产品下钻到型号。'
    result=run(query)
    plan=result['plan']
    assert plan['intent']=='dimensional' and plan['business_context']=={'project':'M'}
    assert plan['drill_role']=='产品'
    assert [s['text'] for s in plan['conditions']]==['华东','路由器','销售收入']
    assert len(plan['conditions'][1]['candidates'])==2
    assert '保密' not in str(plan)
    detail=result['dimensional']
    assert detail['status']=='matched' and detail['fact_ids']==['fact']
    assert detail['children']==['r1']
    assert {n['id'] for n in result['nodes']}=={'fact','rev','east','router','r1'}
    assert detail['witnesses'][0]['row_id']=='c1'


@pytest.mark.parametrize('change',['row','source','coarse','missing'])
def test_topological_common_hub_and_pairwise_matches_cannot_prove_joint_fact(run,change):
    data=graph()
    obs=data['edges'][1]['supports'][0]
    if change=='row':
        obs['row_context']['id']='other'
    if change=='source':
        obs['provenance']['source']['table']='other'
    if change=='coarse':
        for edge in data['edges']:
            item=edge['supports'][0]
            item['provenance']['context_columns']=['project']
            item['row_context']={'id':'M','values':{'project':'M'}}
    if change=='missing':
        obs.pop('row_context')
    result=run('在 M 项目中查询华东的路由器销售收入',fixture=data)
    assert result['dimensional']['status']=='no_common_fact'
    assert not result['edges'] and not result['nodes']


@pytest.mark.parametrize(('query','intent'), [
    ('M 项目的销售收入','search'),
    ('M 项目中销售收入有哪些相关内容','explore'),
    ('未知X项目的销售收入','clarify'),
    ('只看 M 项目，不关心 K 项目。查询华东的销售收入','dimensional'),
    ('只看 M 项目，忽略华东。查路由器销售收入','clarify'),
    ('只看 M 项目，查询华东或者华西的销售收入','clarify')])
def test_intent_and_negation_routes(run,query,intent):
    out=run(query)
    assert out['plan']['intent']==intent
    if intent=='clarify':
        assert out['plan']['unresolved'] and not out['nodes']


@pytest.mark.parametrize('query', ['华安项目的销售收入', 'X项目查销售收入', 'M 项目，AM 项目的销售收入'])
def test_unconfirmed_project_returns_no_context_from_other_projects(run, query):
    result = run(query)
    assert result['plan']['scope_unresolved']
    assert result['available_context']['status'] == 'scope_unresolved'
    assert not result['nodes'] and not result['available_context']['nodes']


def test_structured_conditions_scoped_first_and_no_proxy_paths_on_budget(run):
    params={'mode':'dimensional','business_context':{'project':'M'},'conditions':[
        {'role':'地区','node_id':'east'}, {'role':'指标','node_id':'rev'}, {'role':'产品','text':'路由器'}],
        'max_nodes':2}
    res=run(data=params)
    assert not res['nodes'] and res['dimensional']['status']=='incomplete'
    params['conditions'][0]['node_id']='k'
    assert run(data=params)['dimensional']['status']=='no_match'


def test_unbound_hard_condition_returns_scoped_suggestions_not_false_joint_answer(run):
    out = run('M 项目中查询华东的路由器销售收入，限定直营客户且金额大于100元')
    assert out['plan']['intent'] == 'clarify'
    assert '直营客户' in str(out['plan']['unresolved'])
    assert out['plan']['suggestions'] and out['plan']['suggestions'][0]['status'] == 'suggestion'
    assert not out['nodes'] and '保密' not in str(out['plan'])
    assert 'dimensional' not in out


def test_declared_target_level_and_incompatible_natural_branches(run):
    out = run('M 项目中查询华东路由器销售收入，按产品下钻至型号')
    assert out['dimensional']['level_node_ids'] == ['r1']
    assert out['dimensional']['stop_reason'] == 'level_reached'
    unknown = run('M 项目中查询华东路由器销售收入，按产品下钻到未经声明层级')
    assert unknown['plan']['intent'] == 'clarify' and not unknown['edges']


@pytest.mark.parametrize('mismatch',['','fk_value','key_type','dimension_table','grain'])
def test_typed_fact_to_dimension_lookup_binds_conditions_without_matching_names(run,mismatch):
    data=graph()
    for edge in data['edges']:
        item=edge['supports'][0]
        item['row_context']['values']['region']='005'
    area=data['nodes'][2]['supports'][0]
    source={'source_id':'db','schema':'public','table':'region_dimension'}
    area['provenance']={'source':source,'key_columns':['business_code']}
    fact=data['edges'][1]['supports'][0]
    fact['lookup_matches']=[{'endpoint':'target','source':source.copy(),'match_columns':['region_fk'],
        'input_columns':['region'],'input_values':['005'],'input_value_types':['text'],
        'key_columns':['business_code'],'resolved_key':['E'],'resolved_key_types':['text'],
        'row_context':{'id':'dimension-005','values':{'region_fk':'005','business_code':'E'},
                       'value_types':{'region_fk':'text','business_code':'text'}}}]
    if mismatch=='fk_value':
        fact['lookup_matches'][0]['input_values']=['006']
    if mismatch=='key_type':
        data['nodes'][2]['identity_types']={'code':'decimal'}
    if mismatch=='dimension_table':
        fact['lookup_matches'][0]['source']['table']='other_dimension'
    if mismatch=='grain':
        for edge in data['edges']:
            edge['supports'][0]['provenance']['context_columns'].remove('region')
    out=run('M 项目中查询华东路由器销售收入',fixture=data)
    assert out['dimensional']['status']==('no_common_fact' if mismatch else 'matched')
    if not mismatch:
        edge = next(e for e in out['edges'] if e['id']=='fact-east')
        assert edge['observations'][0]['lookup_matches'][0]['row_context']['id']=='dimension-005'


@pytest.mark.parametrize('query',[
    '我想知道，M 项目的销售收入里，华东这一块的路由器究竟对应哪些型号？',
    '帮我把 M 项目里华东地区的路由器销售收入按型号拆开看看。',
    'M 项目中，华东销售收入里，路由器对应的型号有哪些？',
])
def test_grain_identified_from_visible_types_and_hierarchy_without_drilldown_keyword(run,query):
    out=run(query)
    assert out['plan']['drill_role']=='产品', out['plan']
    assert out['plan']['requested_level']=='型号' and out['dimensional']['level_node_ids']==['r1']


def test_topic_scoped_overview_does_not_silently_answer_with_unfiltered_statistics(run):
    out=run('在 M 项目中只看与华东关联的核心节点')
    assert out['plan']['intent']=='clarify' and not out['nodes']


@pytest.mark.parametrize('query', [
    'M 项目只看华东，限定直营客户且金额大于100元',
    '只看 M 项目在华东售出的交换机净利润',
    'M 项目里的销售收入，须为已经交付且尚未开票的合同，含税额大于十万元',
    'M 项目中查询华东和华西的销售收入',
])
def test_partially_grounded_restrictions_do_not_fall_through_to_search(run,query):
    out=run(query)
    assert out['plan']['intent']=='clarify'
    assert out['plan']['unresolved'] and not out['nodes']


def test_three_conditions_on_one_chain_use_its_middle_node(run):
    data = graph()
    first, second, third = data['edges'][:3]
    first.update(source='east', target='rev')
    first['supports'][0]['provenance']['source_key_columns']=['region']
    third['target']='east'
    third['supports'][0]['provenance']['target_key_columns']=['region']
    data['edges'].remove(second)
    out=run('M 项目中查询华东路由器销售收入',fixture=data)
    assert out['dimensional']['status']=='matched'
    assert {n['id'] for n in out['nodes']} == {'east','rev','router'}
    assert len(out['edges']) == 2


def test_catalog_hierarchy_slice_requires_child_in_the_matched_fact_tuple(run):
    from copy import deepcopy
    data = graph()
    membership=deepcopy(data['edges'][0])
    membership.update(id='fact-r1',target='r1',type='binds_model')
    membership['supports'][0]['provenance']['target_key_columns']=['model']
    data['edges'].append(membership)
    for edge in data['edges'][3:5]:
        obs=edge['supports'][0]
        obs.pop('row_context')
        obs['business_context']={'version':1,'status':'global','dimensions':{}}
        obs['provenance']['source']['table']='product_hierarchy'
    out=run('M 项目中查询华东路由器销售收入，按产品下钻到型号',fixture=data)
    assert out['dimensional']['level_node_ids']==['r1']
    assert {'router-r1','fact-r1'} <= {e['id'] for e in out['edges']}
    assert 'r2' not in {n['id'] for n in out['nodes']}


def test_definition_question_uses_one_grounded_seed_and_compact_neighborhood(run):
    out=run('M 项目的销售收入是什么？')
    assert out['plan']['strategy']=='bounded_neighborhood'
    assert {n['id'] for n in out['nodes']} == {'rev','fact'}
    assert len(out['edges']) == 1
    point=run('M 项目的销售收入，只看这个节点')
    assert point['plan']['strategy']=='exact_node'
    assert [n['id'] for n in point['nodes']] == ['rev'] and not point['edges']


def test_selective_dimension_restricts_two_large_fanouts_inside_sql(run):
    from copy import deepcopy
    data=graph()
    for index in range(160):
        node=deepcopy(data['nodes'][0])
        node.update(id=f'extra{index}',identity={'code':str(index)})
        data['nodes'].append(node)
        for original in data['edges'][:2]:  # M and the popular revenue/region match all 160.
            edge=deepcopy(original)
            edge.update(id=f'000_extra{index}_{original["target"]}',source=node['id'])
            obs=edge['supports'][0]
            obs['row_context']['id']=f'other{index}'
            obs['row_context']['values']['fact_code']=str(index)
            data['edges'].append(edge)
    out=run('M 项目中查询华东销售收入路由器',fixture=data)
    assert not out['truncated']
    assert out['dimensional']['fact_ids']==['fact']
    assert len(out['edges'])==3


def test_output_budget_does_not_turn_natural_query_into_unscoped_search(run):
    result = run(data={'query': '只看 M 项目，查华东的路由器销售收入。', 'max_nodes': 12})
    assert result['plan']['business_context'] == {'project': 'M'}
    assert result['dimensional']['fact_ids'] == ['fact']
