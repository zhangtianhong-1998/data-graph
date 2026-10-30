"""Generate reproducible structural and concept fixtures, without model calls.

Source workbooks/documents and independent Gold live in different directories.
The default output is a new version; an existing directory is never overwritten.
This is a synthetic capability fixture, not evidence about customer data quality.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path

from openpyxl import Workbook

from lightgraph.evaluation.models import GoldGraph
from lightgraph.runtime.config import RunConfig, load_config

# Bound the longer experiment while reserving time and queries for validation
# and publication. Per-response/provider settings still come from the caller.
MODEL_EXPLORATION_BUDGET = {
    'max_calls':240,'max_tokens':None,'max_discovery_steps':20,
    'max_tool_calls':48,'timeout_seconds':300,
}
RUN_EXPLORATION_BUDGET = {
    'max_run_seconds':7200,'max_db_queries':3000,
    'discovery_time_fraction':.75,'discovery_query_fraction':.75,
}

SUBJECTS = (
    ('LEG', '军团级采购'), ('CLA', '冷链物流'), ('REG', '区域分拨'),
    ('WHL', '批发中心'), ('PRC', '深加工厂'), ('FRM', '种植基地'),
    ('GEN', '通用科目'), ('DLV', '配送服务'), ('GRP', '集团合并'), ('RTL', '鲜果连锁'),
)
# Parent direction is declared here, independently of the serialized row paths.
# Cold-chain freight has parents at different depths. Other costs are homonyms.
ITEMS = (
    ('1000','水果经营损益','Fruit operating result',()),
    ('1100','营业收入','Operating revenue',('1000',)),
    ('1110','果品销售收入','Fruit sales revenue',('1100',)),
    ('1111','苹果销售收入','Apple sales revenue',('1110',)),
    ('1112','柑橘销售收入','Citrus sales revenue',('1110',)),
    ('1113','葡萄销售收入','Grape sales revenue',('1110',)),
    ('1114','梨销售收入','Pear sales revenue',('1110',)),
    ('1120','加工品销售收入','Processed product revenue',('1100',)),
    ('1130','配送服务收入','Delivery service revenue',('1100',)),
    ('1200','营业成本','Operating cost',('1000',)),
    ('1210','果品采购成本','Fruit purchase cost',('1200',)),
    ('1220','内部采购成本','Internal purchase cost',('1200',)),
    ('1230','其他费用','Other cost items',('1200',)),
    ('1300','销售毛利','Gross profit on sales',('1000',)),
    ('1400','经营费用','Operating expenses',('1000',)),
    ('1410','采购物流成本','Procurement logistics cost',('1400',)),
    ('1411','干线运输费','Linehaul freight',('1410',)),
    ('1420','仓储费用','Warehousing expense',('1400',)),
    ('1412','冷链运输费','Cold chain freight',('1410','1400')),
    ('1421','库存损耗','Inventory loss',('1420',)),
    ('1430','管理费用','Administrative expense',('1400',)),
    ('1431','人工费用','Personnel expense',('1430',)),
    ('1432','奖金','Bonus',('1430',)),
    ('1433','其他费用','Other administrative items',('1430',)),
    ('1500','营业利润','Operating profit',('1000',)),
)
BY_STEM = {item[0]: item for item in ITEMS}
EN_VARIANTS = {'1412':'Refrigerated freight','1110':'Fresh fruit sales'}
HIERARCHY_HEADER = ['经营主体']
for level in range(1,6):
    HIERARCHY_HEADER += [f'FRUIT_ITEM_L{level}_CODE',f'FRUIT_ITEM_L{level}_CN_NAME',f'FRUIT_ITEM_L{level}_EN_NAME']
CONCEPT_HEADER = ['经营主体','指标编码','指标名称','产品编码','产品名称','渠道编码','渠道名称']
# M01 deliberately occurs in two independent code domains; names can also repeat.
METRICS = (('M01','应收账款占用'),('M02','采购预付款占用'),('M03','销售收入'),('M04','销售毛利'))
PRODUCTS = (('M01','苹果'),('P02','柑橘'),('P03','葡萄'),('P04','梨'),('P05','梨'))
CHANNELS = (('C01','零售渠道'),('C02','批发渠道'),('C03','直营渠道'))
DEFINITIONS = {
    'metric': ('指标定义',['指标编码','指标名称','口径说明'],METRICS),
    'product': ('产品定义',['产品编码','产品名称','产品分类'],PRODUCTS),
    'channel': ('渠道定义',['渠道编码','渠道名称','渠道说明'],CHANNELS),
}


def paths_to(stem):
    parents=BY_STEM[stem][3]
    return [[stem]] if not parents else [p+[stem] for parent in parents for p in paths_to(parent)]


def included_stems(index):
    # Only optional leaves vary; all parents remain present. This is deliberately
    # nonuniform so one sample or cross product cannot recreate the full result.
    omitted={leaf for j,leaf in enumerate(('1111','1112','1113','1114','1120','1130','1210','1220','1431','1432'))
             if (index*3+j)%5 in (0,1)}
    return set(BY_STEM)-omitted


def pad(path):
    return path+[path[-1]]*(5-len(path))


def hierarchy_rows():
    rows=[]
    for subject_index,(prefix,subject) in enumerate(SUBJECTS):
        for stem in sorted(included_stems(subject_index)):
            for path in paths_to(stem):
                if len(path)<2:continue
                for copy in range(2+(subject_index+int(stem))%4):
                    padded=pad(path)
                    # Internal repeated position, followed by a real child.
                    if len(path)==4 and copy%2==1:
                        padded=path[:2]+[path[1]]+path[2:]
                    row=[subject]
                    for level,item in enumerate(padded):
                        _,cn,en,_=BY_STEM[item]
                        if item in EN_VARIANTS and (subject_index+copy+level)%7==0:
                            en=EN_VARIANTS[item]
                        row += [f'{prefix}_PNL_{item}',cn,en]
                    rows.append(row)
    return rows


def context_specs():
    result=[]
    for si,(_,subject) in enumerate(SUBJECTS):
        for mi,(metric,_) in enumerate(METRICS):
            for pi,(product,_) in enumerate(PRODUCTS):
                # Exactly one real channel per subject/metric/product plus a
                # smaller second-channel subset; many pairwise combinations
                # are intentionally absent and must not be manufactured.
                channels={(si+mi+pi)%len(CHANNELS)}
                if (si+2*mi+pi)%5==0:channels.add((si+mi+pi+1)%len(CHANNELS))
                for ci in sorted(channels):
                    result.append({'subject':subject,'metric':metric,'product':product,'channel':CHANNELS[ci][0]})
    return result


def concept_rows():
    names={role:dict(items) for role,items in [('metric',METRICS),('product',PRODUCTS),('channel',CHANNELS)]}
    rows=[]
    for index,c in enumerate(context_specs()):
        row=[c['subject']]
        for role in ('metric','product','channel'):row += [c[role],names[role][c[role]]]
        rows.extend([list(row) for _ in range(1+index%3)])
    return rows


def hierarchy_gold(rows):
    nodes={}
    for row in rows:
        for i in range(5):
            code,cn,en=row[1+3*i:4+3*i]
            node=nodes.setdefault(code,{'code':code,'kind':None,'labels':[],'english_names':[],
                'positions':[],'subjects':[]})
            for key,value in [('labels',cn),('english_names',en),('positions',i+1),('subjects',row[0])]:
                if value not in node[key]:node[key].append(value)
    edges=[]
    for si,(prefix,_) in enumerate(SUBJECTS):
        included=included_stems(si)
        for stem in sorted(included):
            for parent in BY_STEM[stem][3]:
                assert parent in included
                edges.append({'source':f'{prefix}_PNL_{parent}','target':f'{prefix}_PNL_{stem}','relation':'parent_of'})
    gold={'version':'lightgraph.gold/1','nodes':[nodes[k] for k in sorted(nodes)],'edges':edges,
        'namespace_column':'经营主体','entity_kinds':[],'parent_relations':['parent_of','contains'],'child_relations':['child_of']}
    return GoldGraph.model_validate(gold).model_dump(mode='json')


def concepts_gold():
    return {'version':'lightgraph.concepts-gold/1','namespace_column':'经营主体',
        'concepts':[{'role':role,'code':code,'label':label} for role,(_,_,items) in DEFINITIONS.items() for code,label in items],
        'contexts':context_specs(),
        'relation_defaults':{'metric':'binds_metric','product':'binds_product','channel':'binds_channel'}}


def document(identifier,title,tables,text):
    return ('---\ndocument_id: '+identifier+'\ntitle: '+title+'\npurpose: data_description\nscope:\n'
        '  source_ids: [fruit]\n  tables: ['+', '.join('public.'+table for table in tables)+']\n---\n\n'+text.strip()+'\n')


def descriptions():
    docs={}
    docs['报表项层级.md']=document('fruit_v2_hierarchy','水果经营报表项定义',['报表项层级'],'''# 字段与范围
经营主体标出路径属于哪个主体。每个 FRUIT_ITEM_L1 到 FRUIT_ITEM_L5 位置上的 CODE、CN_NAME、EN_NAME 分别为同一报表项的完整编码、中文名称和英文名称。所有位置记录同类的报表项，位置相邻表示父项到子项的方向。

# 对象身份
同一主体内相同完整编码始终指同一报表项；位置不是身份的一部分。不同主体的报表项由各自管理，不能按名称合并。一个主体内也可存在同名异码报表项，例如不同费用类别下的“其他费用”，其编码各自独立。

# 路径与重复
同一报表项可在不同路径深度出现，也可有多个父项。所有非填充的父子关系都保留。连续重复编码是填充，包括末尾补齐和中间重复后还有后继的情况；重复次数不能生成新节点或自环。相同路径可重复录入，不增加身份或边。

# 名称
中文名称直接描述报表项；不得按名称中的业务词推断指标、产品或渠道分解。一个编码可有多种英文写法，均作为该报表项的别名保存，不按英文名拆身份。''')
    docs['指标产品渠道.md']=document('fruit_v2_contexts','指标与产品渠道适用范围',['指标产品渠道'],'''# 独立对象与编码域
指标编码/指标名称、产品编码/产品名称、渠道编码/渠道名称分别描述三种独立对象。指标、产品、渠道各有独立编码域；不同域允许编码文本相同，不能跨类型合并。每个域内相同编码表示同一个对象，名称可有同名异码项，同名不是合并证据。

# 定义和全局范围
指标编码域由指标定义表管理，产品编码域由产品定义表管理，渠道编码域由渠道定义表管理。本表中的同域编码与对应定义表编码指同一对象。三个编码域均为全集团统一管理，概念身份不依赖经营主体；无论在哪个主体出现，同域同码均复用同一概念。经营主体只限制适用组合的来源范围。

# 联合适用范围
一行说明该经营主体下，某指标对该产品经该渠道的经营活动适用。三个概念必须分别建模，整行不作为新的指标或报表项。经营主体、指标编码、产品编码、渠道编码共同确定一个联合适用组合，可用关系或关联记录表示。完全重复的行没有新增事实。

不能将独立出现过的指标、产品、渠道任意组合；例如一个产品可对应不同渠道，但只有源数据中同时出现的三元组才有依据。联合关系必须保留主体范围，不能串配其他主体的组合。本表没有金额或观测值。''')
    texts={
      'metric':'指标编码由全集团统一分配，经营主体不参与指标身份。指标名称用于显示，口径说明区分业务含义；名称或口径不能替代明确编码。',
      'product':'产品编码由全集团统一分配，经营主体不参与产品身份。产品名称可以重名，产品分类辅助解释；相同名称但不同编码的产品不得合并。',
      'channel':'渠道编码由全集团统一分配，经营主体不参与渠道身份。渠道名称用于显示，渠道说明描述服务方式。',
    }
    for role,(name,_,_) in DEFINITIONS.items():
        docs[name+'.md']=document('fruit_v2_'+role,name,[name],
            '# 编码和身份\n'+texts[role]+'该表的编码域只在本类型内生效，不同类型的相同编码没有身份等价关系。\n\n'
            '# 跨表引用\n指标产品渠道表中的同类型编码引用本表编码，代表同一个业务对象。')
    return docs


def definition_rows(role):
    _,_,items=DEFINITIONS[role]
    extras={
      'metric':['赊销余额占用','采购付款尚未结转的占用','销售交易收入','销售收入扣除销售成本'],
      'product':['鲜食苹果','鲜食柑橘','鲜食葡萄','鲜食梨','加工用梨'],
      'channel':['门店零售','经销商批发','企业直营'],
    }
    return [[code,name,extras[role][i]] for i,(code,name) in enumerate(items)]


def write_json(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n',encoding='utf-8')


def write_workbook(path,tables):
    workbook=Workbook();workbook.remove(workbook.active)
    for name,(header,rows) in tables.items():
        sheet=workbook.create_sheet(name);sheet.append(header)
        for row in rows:sheet.append(row)
        sheet.freeze_panes='A2';sheet.auto_filter.ref=sheet.dimensions
    workbook.save(path)


def validate_fixture(tables,hgold,cgold):
    """Check full emitted data independently against declared graph/tuple sets."""
    hierarchy=tables['报表项层级'][1];observed_nodes=set();observed_edges=set();names=defaultdict(set)
    real_depths=defaultdict(set);internal_fill=0
    for row in hierarchy:
        path=[]
        for i in range(5):
            code,cn,en=row[1+3*i:4+3*i]
            assert code and cn and en
            assert '-' not in cn and '—' not in cn
            observed_nodes.add(code);names[code].add(cn)
            if not path or path[-1]!=code:path.append(code)
        for i,code in enumerate(path):real_depths[code].add(i+1)
        observed_edges.update(zip(path,path[1:]))
        codes=row[1::3]
        internal_fill += any(codes[i]==codes[i+1] and any(c!=codes[i] for c in codes[i+2:]) for i in range(3))
    assert observed_nodes=={n['code'] for n in hgold['nodes']}
    assert observed_edges=={(e['source'],e['target']) for e in hgold['edges']}
    assert all(len(values)==1 for values in names.values())
    assert not any(a==b for a,b in observed_edges)
    for role,(_,_,items) in DEFINITIONS.items():
        assert len(dict(items))==len(items)
    contexts={(r[0],r[1],r[3],r[5]) for r in tables['指标产品渠道'][1]}
    expected={(r['subject'],r['metric'],r['product'],r['channel']) for r in cgold['contexts']}
    assert contexts==expected
    assert len(contexts)<len(SUBJECTS)*len(METRICS)*len(PRODUCTS)*len(CHANNELS)
    for row in tables['指标产品渠道'][1]:
        for role,index in [('metric',1),('product',3),('channel',5)]:
            assert dict(DEFINITIONS[role][2])[row[index]]==row[index+1]
    return {'hierarchy_rows':len(hierarchy),'hierarchy_entities':len(observed_nodes),'hierarchy_edges':len(observed_edges),
        'hierarchy_internal_fill_rows':internal_fill,'hierarchy_cross_depth_entities':sum(len(v)>1 for v in real_depths.values()),
        'hierarchy_distinct_rows':len({tuple(r) for r in hierarchy}),
        'hierarchy_entities_by_subject':dict(Counter(n['subjects'][0] for n in hgold['nodes'])),
        'concept_rows':len(tables['指标产品渠道'][1]),'concept_contexts':len(contexts),
        'concept_entities':len(cgold['concepts']),'concept_cartesian_contexts':len(SUBJECTS)*len(METRICS)*len(PRODUCTS)*len(CHANNELS),
        'concept_same_label_different_code':2,'concept_cross_role_code_collision':['M01'],
        'table_rows':{name:len(rows) for name,(_,rows) in tables.items()}}


def generate(output,base_config=None):
    output=Path(output).resolve()
    if output.exists():raise ValueError('dataset_output_exists')
    tables={'报表项层级':(HIERARCHY_HEADER,hierarchy_rows()),'指标产品渠道':(CONCEPT_HEADER,concept_rows())}
    tables.update({name:(header,definition_rows(role)) for role,(name,header,_) in DEFINITIONS.items()})
    hgold=hierarchy_gold(tables['报表项层级'][1]);cgold=concepts_gold()
    stats=validate_fixture(tables,hgold,cgold)
    output.mkdir(parents=True);inputs=output/'inputs';inputs.mkdir();gold=output/'gold';gold.mkdir()
    write_workbook(inputs/'fruit_workbook.xlsx',tables)
    write_workbook(inputs/'hierarchy.xlsx',{'报表项层级':tables['报表项层级']})
    write_workbook(inputs/'concepts.xlsx',{'指标产品渠道':tables['指标产品渠道']})
    for name,(header,rows) in tables.items():
        with (inputs/(name+'.csv')).open('w',newline='',encoding='utf-8') as handle:
            writer=csv.writer(handle);writer.writerow(header);writer.writerows(rows)
    for name,text in descriptions().items():(inputs/name).write_text(text,encoding='utf-8')
    write_json(gold/'hierarchy.json',hgold);write_json(gold/'concepts.json',cgold)
    # Negative probes are evaluator fixtures, deliberately absent from inputs.
    write_json(gold/'counterexamples.json',{
        'missing_contexts':[{'subject':subject,'metric':metric,'product':product,'channel':channel}
            for _,subject in SUBJECTS for metric,_ in METRICS for product,_ in PRODUCTS for channel,_ in CHANNELS
            if {'subject':subject,'metric':metric,'product':product,'channel':channel} not in cgold['contexts']],
        'must_not_merge':[
            {'reason':'same_type_same_label_different_code','left':{'role':'product','code':'P04'},'right':{'role':'product','code':'P05'}},
            {'reason':'different_code_domains','left':{'role':'metric','code':'M01'},'right':{'role':'product','code':'M01'}}],
        'invalid_input_mutations':[
            {'table':'指标产品渠道','row':0,'column':'指标编码','value':None,'expected':'reject_null_concept_identity'},
            {'table':'指标产品渠道','row':0,'column':'产品编码','value':'UNREGISTERED','expected':'unresolved_reference'},
            {'table':'报表项层级','row':0,'column':'FRUIT_ITEM_L1_CN_NAME','value':'冲突名称','expected':'reject_conflicting_label'}]})
    base=load_config(base_config) if base_config else RunConfig(sources=[])
    model=base.resolved_model.model_dump(mode='json');model.update(MODEL_EXPLORATION_BUDGET)
    for mode,workbook,names in [('hierarchy','hierarchy.xlsx',['报表项层级']),
        ('concepts','concepts.xlsx',['指标产品渠道']),('full','fruit_workbook.xlsx',list(tables))]:
        purpose=('识别报表项身份、名称别名、来源位置及父子关系。' if mode=='hierarchy' else
                 '识别独立指标、产品和渠道，以及带经营主体范围的联合适用关系。' if mode=='concepts' else
                 '识别报表项层级及其身份复用；另行识别独立指标、产品、渠道与带主体范围的联合适用关系，引用定义表。')
        config=RunConfig.model_validate({'sources':[{'id':'fruit','kind':'excel_workbook','path':f'inputs/{workbook}'}],
            'documents':[f'inputs/{name}.md' for name in names],'rules':[],
            'goal':{'goal_id':'fruit_v2_'+mode,'purpose':purpose,
                'include':['编码、中文名称和英文别名','有证据支持的身份复用、层级和关系'],
                'exclude':['把同名异码或不同编码域对象合并','把整行或维度组合命名为新的指标','凭两两关系制造未出现的联合组合'],
                'entity_kinds':[],'relation_kinds':[]},
            'scopes':['public']+[name for _,name in SUBJECTS], 'isolation_domain':'fruit_clean_v2_'+mode,
            'output_dir':str(output.parents[1]/'runs'/('fruit-clean-v2-'+mode)),'model':model,
            'limits':base.limits.model_dump(mode='json'),'max_identity_reviews':0,
            **RUN_EXPLORATION_BUDGET})
        serialized=config.model_dump(mode='json',exclude_none=True)
        # Null explicitly disables the cumulative token cap; omitting it would
        # silently restore ModelConfig's finite default when this file is read.
        serialized['model']['max_tokens']=config.resolved_model.max_tokens
        write_json(output/f'config-{mode}.json',serialized)
    hashes={str(p.relative_to(output)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(output.rglob('*')) if p.is_file()}
    write_json(output/'manifest.json',{'version':'fruit_fixture/2','stats':stats,'files':hashes,
        'gold_in_model_inputs':False,'acceptance_boundary':'合成结构与概念能力测试；未运行模型，不代表客户数据效果。'})
    (output/'README.md').write_text('''# 水果合成测试数据 v2

输入在 inputs/，评分真值和反例在 gold/，三个 config 文件只引用输入，不引用 Gold。full 是五表联合发现，hierarchy 只测报表项，concepts 只测独立概念和联合组合。不要用增加组合节点数作为发现能力改善的证据。

报表项重复行、跨位置复用、内部/末尾填充、多个父项、同名异码和英文别名有意保留。独立概念编码跨主体共用，但编码域按指标/产品/渠道区分；同域不同码即使重名也不合并。组合表不是笛卡尔全集，不能按两两边补齐三元关系。主输入没有坏编码/空键/标签冲突；这些负例只在 gold/counterexamples.json 给出，不污染正例。

层级 Gold 按明确完整编码计量，不限制模型使用的英文类型名。概念 Gold 按 role+code 计量，联合组合按主体+三个角色编码计量；实际模型类型/关系映射必须显式记录。Gold 节点数只是该测试合同的预期，不能作为提示词输入。完整数据校验只检查 fixture 自身一致性，不证明模型能正确提取。

config 的模型和单次输出设置沿用准备时指定的本地配置，密钥仅从环境变量读取。三个任务各自总上限 120 分钟、240 次模型调用、3,000 次查询；累计 token 数不限，使用量仍记录。探索最多使用前 90 分钟和 2,250 次查询，余下 30 分钟和 750 次查询留给执行与发布。单任务最多 20 回合、48 次探索工具调用，单请求超时 300 秒；调用次数、时间或查询额度先到限时仍会提前停止。单次生成长度和请求/响应字节限制保留。这些是上限，并不保证花满或达到验收要求。配置可运行，但生成数据不调用模型。生产验收应另外使用未参与设计的客户数据。
''',encoding='utf-8')
    return stats


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',default='evaluation_data/fruit-clean-v2-20261029')
    parser.add_argument('--model-config',help='Optional local run config; copies model settings, never API-key values.')
    args=parser.parse_args()
    print(json.dumps({'output':str(Path(args.output).resolve()),**generate(args.output,args.model_config)},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
