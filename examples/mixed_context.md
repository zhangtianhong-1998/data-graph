---
document_id: mixed_files_context
purpose: mixed
sections:
  通用约定:
    purpose: data_description
  报表项目:
    purpose: data_description
    scope:
      source_ids: [workbook]
      tables: [workbook.public.report_item]
  CSV 引用:
    purpose: data_description
    scope:
      source_ids: [reference_csv]
      tables: [reference_csv.public.item_reference]
  分析建议:
    purpose: analysis_knowledge
---
# 通用约定

编码默认只在租户内有效。核对引用时同时使用租户和编码，不按名称相同直接合并实体。

# 报表项目

item_code 表示租户范围内的报表项目编码，item_name 表示名称。金额是观测值，不属于项目定义。

# CSV 引用

每条记录引用 Excel 报表项目登记表中的项目。tenant_id 与 item_code 一起定位项目，item_name 用于核对引用名称。引用核验通过不单独证明两个来源应共享业务身份。

# 分析建议

分析销售费用前，建议先核对租户、统计期间和项目定义。名称一致只提供查证线索。
