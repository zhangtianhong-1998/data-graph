# 测试机任务：全图节点与关系消歧实验

请在一批冻结数据上一次完成四份抽取子图、合并图、联合重放和独立对账。交付一份报告，回答“现有规则消除了多少、新规则还能消除多少、其中多少有业务依据、哪些仍然判不清”。实验只读原项目，结果留在新的临时目录。沿用已有检查点、规则清单和业务材料，无需开启服务、重新抽取、连源库、调用模型、向量化或发布。

## 开始前

在测试机仓库根目录运行，使用仓库的 .venv。同步本次脚本及两个构图模块；若收到增量包，按包内 manifest 的 LF 归一 SHA-256 核对。代码已直接同步时可直接执行，记录实际模块路径和脚本摘要。正式 8001 和原项目保持只读。

选取隔离项目中**实际提交**产物 artifact_6f2d324a8ff2218e450fd1d72c8bbee4 的运行 run_a408526d13bb45158a32ba98。它复用 extract_1 至 extract_4，归档输入合计 29,462 个节点、34,954 条边，提交图为 23,958 个节点、34,954 条边。城市组织参考组使用已验证的两表城市组织 simulation.json。先看此文件顶层 mode=simulation_only，run_id 和 merge_artifact_id 应与上述值相等。遇目录搬迁，按完整运行 ID 或产物 ID 在 .cac/tmp 下找齐同一归档，不改 ID、检查点或摘要。

## 一条主命令

在 PowerShell 执行下面的代码。按实际位置调整前三个路径。工具会新建结果目录，已有同名结果会被保留。默认同时分析两张图，如果测试机内存有余量，可把 workers 调到 4。工作进程只接收检查点地址，默认不会同时启动四份完整重放。

```powershell
$py = (Resolve-Path '.venv/Scripts/python.exe').Path
$run = (Resolve-Path '.cac/tmp/ict-fusion-test-20261124-220500/ws_iso/ICT/runs/run_a408526d13bb45158a32ba98.json').Path
$known = (Resolve-Path '.cac/tmp/ict-replay-candidates-20261125-181018/simulation.json').Path
$out = Join-Path $PWD ('.cac/tmp/fusion-census-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
$entry = Join-Path $PWD 'scripts/audit_fusion_census.py'
$env:PYTHONPATH = (Resolve-Path 'src').Path
# 如果是通过 ZIP 交付，把 ZIP 放在仓库根目录。只覆盖新建的工具副本。
$zip = Join-Path $PWD 'ict-fusion-census-windows.zip'
if (Test-Path -LiteralPath $zip) {
    $toolsDir = "$out-tools"
    New-Item -Type Directory -Path "$toolsDir/src" -Force | Out-Null
    Copy-Item -LiteralPath 'src/lightgraph' -Destination "$toolsDir/src/lightgraph" -Recurse -ErrorAction Stop
    Expand-Archive -LiteralPath $zip -DestinationPath $toolsDir -Force -ErrorAction Stop
    $entry = Join-Path $toolsDir 'scripts/audit_fusion_census.py'
    $env:PYTHONPATH = Join-Path $toolsDir 'src'
    @'
import hashlib, json, pathlib, sys
root = pathlib.Path(sys.argv[1])
files = json.loads((root / "fusion-census-manifest.json").read_text("utf-8"))["sha256_lf"]
bad = [p for p, h in files.items() if hashlib.sha256((root / p).read_bytes().replace(b"\r\n", b"\n")).hexdigest() != h]
print("files", len(files), "changed", bad)
sys.exit(bool(bad))
'@ | & $py -X utf8 - $toolsDir
    if ($LASTEXITCODE -ne 0) { throw '实验工具指纹不一致，请检查同步文件。' }
}
$familyFile = "$out-families.json"
@'
[{"family":"worksheet_parent_child","types":["indicator_parent_child","indicator_has_child_indicator","report_item_parent_of_report_item"]}]
'@ | Set-Content -LiteralPath $familyFile -Encoding utf8
& $py -X utf8 $entry run $run --node merge_1 --simulation $known --relation-families $familyFile --exclude-text SNULL --workers 2 --output $out
$LASTEXITCODE
```

关系族文件声明的是这次检验的“是否表达同一断言”假设。先核对历史材料中的三种原始关系名。有其他有依据的同义族，可在运行前一次列入，同一类型仅归一个族。所有未列入的关系仍参加全图精确重复、同端点、层级、自环、同名端点及画布统计。多层级关系按各自的物理列对核对。原始行号缺失时记缺证；若本机有同一份冻结工作簿和列序等独立证据，可另列外部核实结果及覆盖分母，保留与程序结果的区别。

如果城市组织清单找不到，但四检查点和合并归档齐全，先按相同命令去掉 `--simulation $known` 做全图实验。报告注明参考清单缺失，单列探索结果，不能把它充当已验证的 50 键对照。

## 并行查看结果

程序结束后，不同审查者可以同时读取以下四路结果。每路给出总量、可判定数、缺证数、第一项缺失证据，并保存成员 ID 在本机。

|分工|查看什么|必须交出的数|
|---|---|---|
|抽取单元内部|四个 unit-* 的 nodes.json。区分同单元不同草稿、同一草稿中重名、同一物理键被多个定义占用。|每单元节点、唯一候选节点、涉及版本化草稿的组、同单元假设折叠量；名称冲突、角色不同、无列证据等拒绝原因。|
|分支间实体|committed/nodes.json、hypotheses.json、baseline.json、reference.json、exploratory.json。|基线逐行是否吻合；已知清单实际净减多少；联合模拟是否比已知清单再少，以及来自哪些类型/表列。列出争用、未覆盖键型、跨族、缺名和证据不足。跨单元假设数、联合规则实测数分别报。multiway.json 单列三个及以上分支在不同物理表上的同时匹配、已经同一输出的组数和假设差额。|
|关系与自环|committed/edges.json、committed/relation-facts.json 和重放的 simulated.relation_facts。|原始边、精确重复、有向端点对、多类型同对、可按证据归一的断言、不同层级、缺行号、自环、画布连线、直接父子对象对数。给出归并节点前后的原始边、断言和画布线差额。|
|独立正确性|各档 input_node_mapping、未核实节点计数、edge_lineage、旧工作簿与目标书。|逐来源的未追回/多匹配边；按下段建立独立应合、应分、未知对照，分别报错合和漏合。|

主报告里 within_only、cross_only、joint 是同物理键假设的三个对照。一个节点可能同时命中多个候选块，各路数字不能相加。reference 和 exploratory 则走同一个正式合并函数的两种内存策略：后者在指定清单的基础上，一次加入所有**当前逐键规则可以表达且不争用**的额外假设。不支持的跨类型、同单元或多方争用仍计入全图普查。若联合规则超过系统上限或不能逐行复现基线，记为“联合重放受阻”，抄录具体规则数/原因，使用已产生的各路全量结果；不得截掉一部分后当作全图净值。当没有额外规则时复用参考结果，不重复运行。图摘要、候选成员和来源明细全部留本机。

## 复核错合和漏合

优先复用本机已有的独立材料。把 50 个真实城市组织码作为已知正对照，两个 SNULL、第三轴同码缺名作为禁止在该规则中折叠的对照；后两者的依据只约束这条规则的适用范围。另列同名异码的保险费、跨表弱键 na_rank、拨备多标签、不同物理层级的同端点边。对缺少业务裁决的实例标 unknown。用两侧完整原始节点 ID 和单元 ID 标注同/异，不靠短 ID、当前系统的结果或模型自评反推真值。

为检测已知样例以外的漏并，按**来源目录/轴、整个模式**划出独立核查集。先用授权的目录、冻结工作簿或带版次的业务定义列出应合/应分对，再查它们是否进过候选块、是否映射到同一对象。覆盖候选外、同单元、跨单元、冲突名与弱键；关系覆盖同义父子、相反方向、多层级、自环。每个有明确规则且可枚举的模式核对该模式的全体成员。理解业务需要人工的，可按分层固定随机种子抽取整组盲判，保存抽样范围与各层分母。已用来编写规则的那 50 个单列为回归，不能当独立泛化得分。无法取得独立依据时交覆盖数与 unknown 数，全图漏并率标“不可估”。对证据不全的候选保留类型、表列、方向和待补的依据。

支持在同一结果上反复评分，无需再跑图。新建 truth.json，顶层为 `run_id`、`artifact_id` 和 `pairs`。每项含 `kind`、`left`、`right`、`expected`（together / separate / unknown）、`stratum`、`evidence_ref`。input_entity 的两端用 `["extract_2","完整原节点ID"]` 这种地址，可以检出原先已经错合的两项。entity 用两个提交图节点 ID；relation_fact 用两个提交图原边 ID，评价所声明的同义关系假设；canvas_line 也用原边 ID，但评价是否应该显示为同一条线。两种关系问题分别评分。程序用可回链的原边对照联合重放后的连线和断言；去向不唯一时列为未核实。accuracy.json 同时报告已知应合实体对是否落入候选块，各模式的错合/漏合可追到具体对象对。

```powershell
& $py -X utf8 $entry score $out --truth "$out/truth.json" --result "$out/accuracy.json"
```

## 交付

报告首页列出三个档位：当前提交图、已知 50 键模拟、扩展规则联合模拟。每档报节点数、原始边数、可区分断言数、默认连线数、相对基线差额、未核实数。另列同单元/跨单元物理键假设，明确哪些只经过证据筛查、哪些已进入正式合并器模拟。

城市组织参考组预期 23,908 个节点、34,954 条原始边；校验历史 113 条关联边可追回，59 条因规则更换 ID，54 条保持 ID/类型/端点，其中含 4 条哨兵边。若不符先检查所选运行、输入及命令，保留实际结果。探索实验数据与预期无目标差额要求。写明哪些模式贡献了额外净减少、哪些会引入错合、剩余歧义的主要来源和各自业务核查成本。给出唯一结果目录和主要命令耗时，只回传汇总与必要脱敏证据。正式工作台仍显示已发布的图。
