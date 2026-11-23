"""Exercise construction, indexing and the public request contract with small source tables."""
import openpyxl
import pytest

from lightgraph.construction.executor import execute_draft
from lightgraph.contracts import ColumnSpec, TableSpec
from lightgraph.retrieval import SubgraphRetriever, sync_graph_to_retrieval
from lightgraph.retrieval.agent_context import agent_context
from lightgraph.retrieval.query import RetrieveSubgraphRequest
from lightgraph.sources import MemorySource
from lightgraph.sources.excel import ExcelSource


def table(name, columns):
    return TableSpec(source_id='s', name=name, columns=[ColumnSpec(name=c) for c in columns])


def index(tmp_path, source, draft):
    with source:
        validated = execute_draft(draft, [source], source.describe(), label_conflict_policy='retain_variants')
    assert validated['passed'], validated['diagnostics']
    graph = validated['graph']
    sync_graph_to_retrieval(graph, tmp_path / 'idx')
    return graph, SubgraphRetriever(tmp_path / 'idx')


def query(retriever, **kwargs):
    return RetrieveSubgraphRequest.model_validate(kwargs).run(retriever, ['public'])


def entity(kind, mapping, columns, *, label=None, keys=None):
    return {'type': kind, 'key_names': keys or ['id'], 'mappings': [
        {**mapping, 'key_columns': columns, 'label_column': label or columns[-1]}]}


def relation(kind, src, dst, mapping, src_cols, dst_cols):
    return {'type': kind, 'source_type': src, 'target_type': dst, 'mappings': [
        {**mapping, 'source_key_columns': src_cols, 'target_key_columns': dst_cols}]}


@pytest.mark.parametrize('physical,overlap', [(True,False), (True,True), (False,False), (False,True)])
def test_joint_fact_needs_a_record_supporting_both_qualified_relations(tmp_path, physical, overlap):
    columns = ['ticket','person','team','received','closed']
    data = [('T1','张三','一组','Y','N'), ('T1','张三','一组','N','Y')]
    if overlap:
        data.append(('T1','张三','一组','Y','Y'))
    if physical:
        path = tmp_path / 'events.xlsx'
        book = openpyxl.Workbook()
        book.active.title = 'events'
        for row in [columns, *data]:
            book.active.append(row)
        book.save(path)
        book.close()
        source = ExcelSource(path, source_id='s')
    else:
        t = table('events', columns)
        source = MemorySource([t], {t.qualified_name: data})
    mapping = {'source_id':'s','table':'events','context_columns':columns[:3],
               'business_context':{'status':'global'}}
    entities = [entity(typ, mapping, [key]) for typ,key in
                [('工单','ticket'),('人员','person'),('团队','team')]]
    relations = [relation(verb, '工单', kind, {**mapping,'predicate':{
        'column':flag, 'op':'eq','value':'Y'}}, ['ticket'], [col])
        for verb,kind,col,flag in [('受理','人员','person','received'),('办结','团队','team','closed')]]
    _, ret = index(tmp_path, source, {'draft_id':'test','entities':entities,'relations':relations})
    result = query(ret, mode='dimensional', conditions=[{'text':'张三'},{'text':'一组'}])
    proven = physical and overlap
    assert result['dimensional']['status'] == ('matched' if proven else 'no_common_fact')
    evidence = agent_context(result)['joint_witnesses']
    assert bool(evidence) == proven
    if proven:
        assert evidence[0]['common_record']['basis'] == 'physical_source_row'
        assert evidence[0]['common_record']['worksheet_row'] == 4
    # A cross-record association can still be returned with its narrower semantics.
    path = query(ret, query='从张三到一组有哪些关联路径？')['traverse']['paths'][0]
    assert path['observed_path'] is False


def test_scoped_names_constrain_automatic_fact_and_traversal_but_not_explicit_ids(tmp_path):
    dictionary = table('areas', ['project','code','name'])
    facts = table('sales', ['project','code','fact','metric'])
    mapping = {'source_id':'s','business_context':{'status':'scoped','columns':{'project':'project'}}}
    lookup = {**mapping,'table':'areas'}
    sales = {**mapping,'table':'sales','context_columns':facts.column_names}
    region = entity('地区',lookup,['code'],label='name')
    region.update(identity_scope='global',identity_domain='地区目录',identity_evidence_refs=['e1'])
    draft = {'draft_id':'test', 'entities':[region,entity('指标',sales,['metric']),entity('订单',sales,['fact'])],
             'relations':[relation('观察'+typ,'订单',typ,sales,['fact'],[key])
                          for typ,key in [('地区','code'),('指标','metric')]]}
    source = MemorySource([dictionary,facts], {
        dictionary.qualified_name:[('M','E','华东'),('K','E','华南')],
        facts.qualified_name:[('K','E','F1','销售收入')]})
    graph, ret = index(tmp_path, source, draft)
    for term, success in [('华东',False),('华南',True)]:
        fact = query(ret, query=f'查询{term}的销售收入')
        path = query(ret, query=f'从{term}到销售收入有哪些关联路径？')
        assert (fact['dimensional']['status']=='matched') == success
        assert bool(agent_context(fact)['joint_witnesses']) == success
        assert (path['traverse']['status']=='found') == success
        if success:
            assert fact['dimensional']['witnesses'][0]['business_context']['dimensions'] == {'project':'K'}
    ids = {n['type']:n['id'] for n in graph['nodes']}
    result = query(ret, mode='dimensional', conditions=[{'node_id':ids['地区']},{'node_id':ids['指标']}])
    assert result['dimensional']['status'] == 'matched'


def test_composite_mapping_keeps_its_declared_order_for_heterogeneous_keys(tmp_path):
    columns = ['tenant_id','product_id','region','ticket']
    facts = table('sales', columns)
    base = {'source_id':'s','table':'sales','context_columns':columns,'business_context':{'status':'global'}}
    draft = {'draft_id':'test','entities':[
        entity('产品',base,columns[:2],keys=['租户','商品']),entity('地区',base,['region']),entity('订单',base,['ticket'])],
        'relations':[relation('商品行','订单','产品',base,['ticket'],columns[:2]),
                     relation('发生地','订单','地区',base,['ticket'],['region'])]}
    source = MemorySource([facts], {facts.qualified_name:[('10',10,'North','T1')]})
    g, ret = index(tmp_path, source, draft)
    ids = {n['type']:n['id'] for n in g['nodes']}
    request = dict(mode='dimensional',conditions=[{'node_id':ids['产品']},{'node_id':ids['地区']}])
    result = query(ret, **request)
    assert result['dimensional']['fact_ids'] == [ids['订单']]
    assert result['dimensional']['witnesses'][0]['common_record']['basis'] == 'common_typed_projection'
    # A stale or incorrect endpoint declaration with reversed physical columns
    # does not bind the typed logical key even when both values print as 10.
    edge = next(e for e in g['edges'] if e['type']=='商品行')
    edge['supports'][0]['provenance']['target_key_columns'].reverse()
    sync_graph_to_retrieval(g, tmp_path / 'other')
    assert query(SubgraphRetriever(tmp_path / 'other'), **request)['dimensional']['status'] == 'no_common_fact'


def test_hierarchy_drilldown_crosses_adjacent_rows_without_asserting_a_joint_record(tmp_path):
    tree = table('tree',['parent','child'])
    base = {'source_id':'s','table':'tree','business_context':{'status':'global'}}
    nodes = entity('组织',base,['parent'])
    nodes['mappings'] += entity('组织',base,['child'])['mappings']
    edge = relation('包含','组织','组织',{**base,'context_columns':['parent','child']},['parent'],['child'])
    edge['hierarchy_orientation'] = 'parent_to_child'
    g, ret = index(tmp_path, MemorySource([tree], {tree.qualified_name:[('集团','分部'),('分部','车间')]}),
                   {'draft_id':'test','entities':[nodes],'relations':[edge]})
    ids = {n['label']:n['id'] for n in g['nodes']}
    for target in [None, ids['车间']]:
        result = query(ret, mode='drilldown', seed_ids=[ids['集团']], target_id=target, relation_types=['包含'])
        path = result['drilldown']['paths'][0]
        assert path['node_ids'] == [ids[k] for k in ['集团','分部','车间']]
        assert path['stop_reason'] == ('target_reached' if target else 'leaf')
        assert path['observed_path'] is False
