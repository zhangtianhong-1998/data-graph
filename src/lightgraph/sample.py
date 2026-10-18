"""Synthetic fixtures for execution gate E. Never imported by automatic discovery."""
from __future__ import annotations
from pathlib import Path
from .contracts import *

DESCRIPTION = '''---
purpose: data_description
applies_to: [public.report_item, public.report_tree, public.measure_registry, public.metric_registry, public.metric_measure_map, public.region_dictionary, public.product_dictionary]
---
# 报表项目登记表
report_item 的 item_code 是租户范围内编码，item_name 是中文名称，item_en_name 是英文别名。definition 表示定义。sort_order 仅用于显示排序，amount 是观测值，不属于定义。同一完整定义可由多个租户引用；编码只在来源内定位。
# 宽表层级
report_tree 的 l1 到 l5 依次为父到子层位。末级重复编码与相同名称表示填充，连续填充合并；空层表示信息缺失，不跨空层补父子关系。分类体系为财务报表项目。
# 指标与度量
measure_registry 登记度量，metric_registry 登记指标。名称、definition、unit 分别表示名称、定义和单位。metric_measure_map 显式记录指标使用哪个度量。
# 维度字典
region_dictionary 登记区域维度成员。region_code 是租户内的成员编码，region_name 是成员名称，parent_code 指向同租户内的父成员编码；空父编码表示根节点。product_dictionary 登记产品维度成员，product_code 是编码，product_name 是中文名，product_en_name 是英文名。
'''
ANALYSIS = '''---
purpose: analysis_knowledge
applies_to: [public.report_item]
---
# 销售费用分析
当销售费用上升时，建议按区域检查费用构成。该建议不证明费用增长由区域变化造成。
# 收入分析
收入下降可能与产品结构变化有关。分析时应先核对统计期间和口径，再比较地区与产品。
'''


def build_sample(total_rows=5000, source_id="synthetic"):
    defs={
      "report_item": ["tenant_id","item_code","item_name","item_en_name","definition","sort_order","amount","private_note"],
      "report_tree": ["tenant_id"]+[f"l{i}_{s}" for i in range(1,6) for s in ("code","name")]+["amount"],
      "measure_registry":["tenant_id","measure_code","measure_name","definition","unit"],
      "metric_registry":["tenant_id","metric_code","metric_name","definition","unit"],
      "metric_measure_map":["tenant_id","metric_code","measure_code"],
      "region_dictionary":["tenant_id","region_code","region_name","parent_code"],
      "product_dictionary":["tenant_id","product_code","product_name","product_en_name"],
      "observation_log":["event_id","tenant_id","description","amount","created_at","private_note"],
    }
    seed={
      "report_item":[["A","2150","销售费用","Selling expense","企业为销售商品发生的费用",112,100,"RETENTION_CANARY_9f2d17"],
                     ["B","B2150","销售费用","Sales cost","企业为销售商品发生的费用",113,240,"RETENTION_CANARY_9f2d17"],
                     ["A","3100","管理费用","Administration expense","管理活动产生的费用",10,90,"RETENTION_CANARY_9f2d17"]],
      "report_tree":[["A","3","经营活动付现利润","1","贡献利润","1","贡献利润","1","贡献利润","1","贡献利润",100],
                     ["A","3","经营活动付现利润","2","非付现调整项目","27","拨备","271","退款负债","2711","收入拨备1",200],
                     ["A","3","经营活动付现利润","2","非付现调整项目","27","拨备","271","退款负债","2713","其中：退款负债",300],
                     ["A","3","经营活动付现利润",None,None,"26","减值损失","261","坏账准备","261","坏账准备",50]],
      "measure_registry":[["A","M1","累计金额","统计期间内金额累计","元"],["A","M2","累计同比","本期累计相对上年同期增幅","百分比"]],
      "metric_registry":[["A","K1","收入","符合收入确认口径的销售收入","元"]],
      "metric_measure_map":[["A","K1","M1"]],
      "region_dictionary":[["A","R0","区域组织",None],["A","R1","亚太地区部","R0"],["A","R2","泰国代表处","R1"]],
      "product_dictionary":[["A","P1","路由器","Router"],["A","P2","无线网络","Wireless network"]],
      "observation_log":[["E1","A","临时测试日志",123,"2026-01-01","RETENTION_CANARY_9f2d17"]],
    }
    tables=[];rows={};per=max(5,total_rows//len(defs))
    for name,columns in defs.items():
        tables.append(TableSpec(source_id=source_id,name=name,columns=[ColumnSpec(name=c,comment=c) for c in columns]))
        rows[f"public.{name}"]=[dict(zip(columns,seed[name][i%len(seed[name])])) for i in range(per)]
    return tables,rows


def sample_rules(source_id="synthetic"):
    def entity(rid,table,code,label,kind="ReportItem",alias=None,definition=None,unit=None,shared=False,parent=None):
        attrs={"label":label}
        if definition:attrs["definition"]=definition
        if unit:attrs["unit"]=unit
        r=RuleSpec(rule_id=rid,kind="long_hierarchy" if parent else "column_group_entity",
            source=SourceRef(source_id=source_id,table=table),scope=ScopeSpec(namespaces=["tenant_id"]),
            identity=IdentitySpec(key_columns=["tenant_id",code],entity_kind=kind,
                semantic_resolution="verified_definition_registry" if shared else "source_scoped",
                semantic_namespace="financial-report" if shared else ""),
            attributes=AttributeSpec(single_valued=attrs,multi_valued={"alias":alias} if alias else {}),
            evidence=RuleEvidence(semantic_basis=["fixture:E:fixed_rule"]),
            hierarchy=HierarchySpec(parent_column=parent,taxonomy="region",order_evidence=["fixture:E:explicit_parent"]) if parent else None)
        return r
    rules=[entity("items","report_item","item_code","item_name",alias="item_en_name",definition="definition",shared=True),
           entity("measures","measure_registry","measure_code","measure_name","Measure",definition="definition",unit="unit"),
           entity("metrics","metric_registry","metric_code","metric_name","Metric",definition="definition",unit="unit"),
           entity("regions","region_dictionary","region_code","region_name","DimensionMember",parent="parent_code"),
           entity("products","product_dictionary","product_code","product_name","DimensionMember",alias="product_en_name")]
    rules[0].attributes.excluded=[ExcludedColumn(column="sort_order",reason="display_order"),ExcludedColumn(column="amount",reason="observation_value"),ExcludedColumn(column="private_note",reason="excluded")]
    rules.append(RuleSpec(rule_id="tree",kind="wide_hierarchy",source=SourceRef(source_id=source_id,table="report_tree"),
        scope=ScopeSpec(namespaces=["tenant_id"]),identity=IdentitySpec(key_columns=["tenant_id","l1_code"],entity_kind="ReportItem",level_policy="code_with_fill_merge"),
        hierarchy=HierarchySpec(levels=[LevelSpec(level=i,code=f"l{i}_code",label=f"l{i}_name") for i in range(1,6)],taxonomy="finance",fill_semantics_confirmed=True,order_evidence=["fixture:E:documented_levels"]),
        evidence=RuleEvidence(semantic_basis=["fixture:E:documented_hierarchy"])))
    rules.append(RuleSpec(rule_id="metric_measures",kind="reference",source=SourceRef(source_id=source_id,table="metric_measure_map"),
        scope=ScopeSpec(namespaces=["tenant_id"]),identity=IdentitySpec(key_columns=["tenant_id","metric_code"],entity_kind="Metric"),
        relation=RelationSpec(source_rule_id="metrics",target_rule_id="measures",source_columns=["tenant_id","metric_code"],target_columns=["tenant_id","measure_code"],relation="uses_measure"),
        evidence=RuleEvidence(semantic_basis=["fixture:E:explicit_mapping"])))
    return rules


def write_sample(directory,total_rows=5000):
    import yaml
    from openpyxl import Workbook
    directory=Path(directory)
    if directory.exists() and any(directory.iterdir()):
        from .errors import LightGraphError
        raise LightGraphError("sample_directory_not_empty")
    directory.mkdir(parents=True,exist_ok=True)
    tables,rows=build_sample(total_rows,source_id="excel")
    wb=Workbook();wb.remove(wb.active)
    for table in tables:
        sheet=wb.create_sheet(table.name);sheet.append(table.column_names)
        for row in rows[table.qualified_name]:sheet.append([row[c] for c in table.column_names])
    wb.save(directory/'input.xlsx')
    (directory/'data_description.md').write_text(DESCRIPTION)
    (directory/'analysis.md').write_text(ANALYSIS)
    (directory/'rules.yaml').write_text(yaml.safe_dump([r.model_dump(mode="json",by_alias=True) for r in sample_rules("excel")],allow_unicode=True,sort_keys=False))
    return {"tables":len(tables),"rows":sum(map(len,rows.values())),"purpose":"gate_E_synthetic"}
