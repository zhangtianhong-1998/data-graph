# 列定义对象及其与实际行的关系

用户目标需要“聚合方式”“度量定义”等内容时，来源可能是字段元数据或知识文档，而不是事实表中的每个数值。先确认这些定义与目标有关、对应的物理列已获授权。没有单独的定义表不妨碍列定义建模。

## 列定义对象

读取 `inspect_source(table, operation="get_metadata")` 或完整适用文档章节，取得实际签发的引用，再读取 `draft_schema`。以下仅示意语法，表、列、类型和证据必须按当前数据填写：

```yaml
draft_id: semantic_analysis
entities:
  - type: MeasureDefinition
    key_names: [source_id, schema, table, column]
    mappings:
      - kind: metadata_column
        source_id: facts
        schema: public
        table: observations
        column: comparison_total
        label: 去年同期季度完成
        aliases: [Previous-year quarter-to-date]
        semantic_annotations:
          aggregation_description: 去年同期季度累计
        evidence_refs: [e1]
```

`key_names` 固定为这四个物理位置标识。不能用 `key_columns: [comparison_total]` 代替，那会把每个金额值当成对象。列对象不查询该列的事实数值，空表也有列定义。来自不同来源或表的同名列默认是不同定义对象。跨列等价合并需另外提供身份解释，本算子不自动合并。

`label`、`aliases` 和 `semantic_annotations` 是 Agent 据证据声明的含义，输出保留其来源；目录类型、空值约束和注释另行保存。未提供 label 时用目录注释，再退到列名。不根据列名自动翻译或覆盖相互矛盾的文档。禁止用语义属性覆盖程序记录的 `physical_column`、`data_type`、`nullable`、`catalog_comment`。

引用必须已签发且适用于该表和列。查询结果和先前验证编号不能代替定义证据；`document_version` 不能代替已读完整章节。引用存在只证明出处可追溯，不等于 Agent 的业务解释经过独立审查。

## 行对象与列定义的关系

在同一草稿中先定义实际行对象和列对象，再添加关系。列端使用 `target_metadata`（或反方向的 `source_metadata`），另一端仍使用实际行键或已有显式查找：

```yaml
type: uses_measure
source_type: Indicator
target_type: MeasureDefinition
mappings:
  - source_id: facts
    schema: public
    table: observations
    source_key_columns: [indicator_code]
    target_metadata:
      source_id: facts
      schema: public
      table: observations
      column: comparison_total
      evidence_refs: [e1]
    metadata_basis: schema_applies_to_rows
```

列端必须与关系输入表对应；对应列对象也必须在草稿中定义。列端不填写 key_columns 或 lookup，不能同时声明两种身份。行端如需从另一表匹配已知指标，仍用 `source_lookup` 明确匹配列、目标身份列和限定条件。

`metadata_basis` 必须明确：

- `schema_applies_to_rows`：依据元数据或文档声明该定义适用于当前筛选范围中的实际行对象。存在该行与适用性声明分别保留，不宣称实际数值非空。
- `observed_non_null`：只连接该列非空的实际行对象。执行器将非空条件下推 SQL，不把经营数值读出来当身份或放入图谱。NULL 不足以证明某个度量在定义上不适用。

两种方式均保留实际行身份，不能把全图所有指标与全部列对象相乘。谓词和行上下文继续适用；需要限定非空维度身份时参阅 `reference:predicate-guide.md`。纯文档虚构的行身份、两个任意列定义之间的关系，不由这个行配对算子表达。

## 覆盖目标并检查结果

按目标分别检查对象定义、指标到维度的实际配对、指标到列定义的适用性或非空观测，以及跨表匹配。文档中的字段引用是关系候选依据，不自动证明事实外键。不能因为只观察到一个指标就宣布关系无须建立；也不能把一个指标的证据推广到其他指标。

结构校验不读事实；实例校验时，列对象只查授权目录，行关系仍需完整执行其显式范围。列对象可见范围是目录的 public；事实关系沿用行支持的 scope，不根据业务编码猜可见权限。检查发布图中的来源、关系依据和统计，区分列定义个数、实际行对象数与关系数。

## 事实来源中断后保留定义

混合草稿含有失败的事实映射时，不能把其中的候选节点数当成发布结果。使用 `save_graph_draft(metadata_from_draft="已保存草稿编号")` 可从该修订另存列定义草稿，随后用返回的 draft_id 执行 instances 验证和发布；这一步不发起事实查询。原草稿、关系映射和未决事项继续保留。没有列定义映射时，这个入口不会根据数值列名猜造定义。

例如，来源中断后仍可发布有依据的“去年同期季度完成”度量定义，以及“地区”维度定义。但地区成员、指标在各地区的配对、哪些指标适用该度量，仍需要各自的事实或适用性证据与验证。恢复后继续验证原草稿。已确认的目录只代表本次读取时的结构；已报告目录变化或权限失效时，不能再用旧目录发布定义。

新运行确认来源恢复后，读取原主草稿并验证其中保留的关系映射，再发布通过验证的版本。仅重验纯列定义派生草稿不会生成关联边。同次验证中，程序可复用来源、实际投影列与有效筛选条件相同的完整结果，每条关系仍保留自己的列端依据；无需为复用手工增加投影列，更不能删掉必要的非空条件或来源上下文。
