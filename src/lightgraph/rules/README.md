# 层级规则与根视图

`level_qualified` 和 `code_with_fill_merge` 保留原有行为。不同记录中的 L1/L2 表示相对路径位置时，使用 `IdentitySpec.level_policy="relative_path"`，并声明以下内容：

- `semantic_resolution="verified_identity_domain"`、`identity_domain` 和 `identity_evidence`：明确业务键所属身份域及其依据。同名、同码、同类型本身不是共享依据。
- `attribute_evidence`：来源属性可以支持共享实体的独立依据。
- `identity_scope="namespace_local"`：默认保留租户等命名空间值。跨租户业务键已验证为全局键时才使用 `domain_global`，并提供 `global_scope_evidence`。
- `HierarchySpec.taxonomy`、`order_evidence`、`position_semantics` 和 `position_evidence`：分别说明层级体系、父子顺序和编号含义。`ordinal_slots` 把 L1/L3 视为缺 L2，按缺口切段；`ordered_slots` 表示编号只用于排列现有槽位。NULL 始终切段。
- `fill_semantics_confirmed`：只控制同一路径连续重复实体能否压缩。跨记录位置变化不需要填充依据。非连续回到相同实体拒绝发布。

源绑定仍按来源和实际代码列组隔离。已验证身份域内的绑定可以共享实体；实体 ID 不含路径深度或 taxonomy。源位置保存为 `source_position` 属性事实，以 taxonomy、source_group、可选 semantic_level 以及业务条件限定；这些限定不进入 `parent_of` 边的去重键。`context_qualifiers` 支持 condition、valid_period、period、hierarchy_version。发布时在各上下文内检查当前有效边与替换边的合并结果；`allow_dag` 允许多父，不能允许环。

Agent 的 `WideHierarchy.path_semantics="relative_path"` 提案使用对应的域、体系、位置和证据字段。编译器要求引用来自当前表适用、完整读取的定义文档；分析经验、其他表的绑定文档和模型编造引用不能批准身份共享。模型给出的身份域名称与业务键、属性授权、层位含义还须有引用正文支持。固定规则中的证据字段由操作方声明，执行器不会把字符串本身重新做自然语言验证。

```python
from lightgraph.rules import HierarchyView

view = store.hierarchy_view(
    {"authorized_tenant"},
    HierarchyView(taxonomy="finance", root_entity=entity_id, max_depth=3),
)
```

返回值包含子树节点的 `display_depth`（入口为 0）、业务边和保留源位置的 `memberships`。查询不重抽数据、不调用模型、不修改父子事实。指定业务条件或版本时在 `context_qualifiers` 中完整给出。

迁移使用同一逻辑规则的新修订完整重投影，并在同一发布组中替换依赖闭包。`identity.migration.plan_identity_migration` 生成旧发布到新发布的候选对照，只匹配同源、同命名空间、同局部编码及对应列组；属性冲突保持未决。`GraphStore.publish_partitions` 在原子事务内核验候选并写入 `identity_migration` 审计，保留旧发布、旧绑定和旧支持。外部边仍引用旧实体而未纳入重算时，完整端点检查会拒绝发布。撤回一个来源只撤回其自身支持。
