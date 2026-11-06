# 实例范围与缺失值

`predicate` 已支持 NULL 判断、比较、集合和逻辑组合。它可以写在实体映射、关系映射、`source_lookup` 或 `target_lookup` 上，不局限于等值条件。读取本材料使用 `read_material(name="reference:predicate-guide.md")`。

`predicate: null`、省略 `predicate` 或空对象 `{}` 都表示不额外筛选。条件中的列使用授权的物理列名，值由程序绑定为 SQL 参数。不要把 `WHERE ...` 字符串、SQL 表达式或猜测的列 ID 填入 `predicate`。

## 缺失值应怎样处理

先确认缺失的是身份、名称还是可选属性。某些行的维度编码缺失，不说明该维度在其他行不存在，也不说明名称可以代替编码。名称、注释、知识文档与事实数据有矛盾时，核对字段角色、空值分布、编码到名称的依赖和适用范围；不能只按列名裁决。

一个对象的身份键含 NULL 或空字符串 `''` 时，实例校验会返回 `null_identity_key`，不会默认删掉该行或自动换键。可选属性的 NULL 不要求排除整个对象。若依据目标和数据说明，可以确认“无编码的记录不提供这个维度实例”，在该维度的映射上显式声明范围，并记录依据、排除数量以及尚未满足的要求。其他对象的映射不必跟着缩小。

NULL、空字符串和空白字符串不同：

- `is_null` 只选择 SQL NULL，不选择 `''`。
- `is_not_null` 只排除 SQL NULL，仍保留 `''`；兼容写法 `not_null` 与它相同。
- `eq` / `ne` 搭配 `value: ''` 用于空字符串。SQL 比较遇到 NULL 不会得到真，不能用 `ne` 表达“包含 NULL”。
- `' '`、`'NULL'`、`'N/A'` 都是普通字符串。没有业务依据时不要自动清洗、合并或当成 NULL。
- `eq` / `ne` 搭配 `value: null` 不合法，诊断为 `predicate_null_requires_explicit_operator`。NULL 判断没有 `value` 字段。

例如，已经确认只排除缺失编码的记录：

```yaml
predicate:
  column: officecode
  op: is_not_null
```

只有当空字符串也已确认表示缺失编码时，才采用更窄的范围：

```yaml
predicate:
  all:
    - column: officecode
      op: is_not_null
    - column: officecode
      op: ne
      value: ''
```

同一条件要覆盖需要它的实体与关系映射。只过滤实体、仍让关系读取无编码行，会留下缺失端点。使用查找时也要分别说明关系输入范围和目标表的查找范围；目标表过滤不会自动过滤关系输入行。`on_missing: omit` 是另一项显式决定，不能用它掩盖尚未解释的缺失引用。

## 完整 YAML 例子

下面是说明语法的虚构表，实际名称和类型应由本次目标与证据确定。假设已确认：`metriccode` 是每行都有的指标编码；`officecode` 可以为 NULL，但非空值都不是空字符串；`office_name` 是名称。没有区域编码的行仍提供指标，只不提供区域实体与归属边。

```yaml
draft_id: optional_dimension_example
purpose: 保留指标及有编码依据的区域关系
entities:
  - type: Indicator
    key_names: [code]
    mappings:
      - source_id: sample
        table: facts
        key_columns: [metriccode]
        label_column: metricname
  - type: Office
    key_names: [code]
    mappings:
      - source_id: sample
        table: facts
        key_columns: [officecode]
        label_column: office_name
        predicate:
          column: officecode
          op: is_not_null
relations:
  - type: belongs_to
    source_type: Indicator
    target_type: Office
    mappings:
      - source_id: sample
        table: facts
        source_key_columns: [metriccode]
        target_key_columns: [officecode]
        predicate:
          column: officecode
          op: is_not_null
decisions:
  - 本例假设已确认NULL区域编码表示该行未提供区域对象；指标仍按metriccode保留。
  - Office使用officecode作身份，office_name仅作主名称；示例结论不可替代本次数据验证。
```

保存草稿后，先用 `validate_graph_draft(draft_id, mode="structure")` 检查列、权限和条件语法；这一步不查事实，不能证明排除范围正确。再按用户要求验证实例。输出应说明本次范围与缺失数量，不把“过滤后的图通过”当成原始数据完整无缺。如果用户要求覆盖这些缺失记录而仍无可确认身份，保留已有结构和有效对象，并在 `unresolved` 记录缺口。

## 可用条件

| 写法 | 含义 | 额外字段 |
| --- | --- | --- |
| `is_null`、`is_not_null` | NULL 判断 | 无 `value` / `values` |
| `eq`、`ne`、`gt`、`ge`、`lt`、`le` | 相等、不等及大小比较 | `value` |
| `in`、`not_in` | 值集合 | `values` 列表，不包含 NULL |
| `between` | 数值或日期的闭区间 | `values: [下界, 上界]` |
| `prefix` | 文本前缀 | `value`；`%`、`_` 等按文字转义 |
| `all` | 所有子条件都满足 | 子条件列表 |
| `any` | 任一子条件满足 | 子条件列表 |
| `not` | 对一个子条件取反 | 一个子条件对象 |

叶子条件通常写成 `{column: 实际列名, op: 操作符, value: 值}`。`in`、`not_in`、`between` 使用 `values`；NULL 操作符仅有 `column` 和 `op`。兼容别名还有 `and` → `all`、`gte` → `ge`、`lte` → `le`，建议新草稿统一使用表中写法。

值的类型须与物理列类型一致，文本编码应加引号保留前导零。当前规则最多三层、32 个条件节点，每个逻辑列表最多 16 项，每个值集合最多 64 项，整次条件最多 128 个绑定值。日期使用 ISO 格式，`between` 不支持文本范围。混合类型列暂不支持此条件语言，需先核对源类型与目标，不得强制转型来通过校验。

过滤条件只能缩小授权范围，不能扩大列权限或绕过排除列。来源查询超时、断线、权限不足与身份缺失是不同问题：先读具体诊断，不能为了通过校验删去仍需完成的维度、改用名称作身份，或把失败来源的范围悄悄排除。
