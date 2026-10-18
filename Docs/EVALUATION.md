# 固定任务集与评估入口

本阶段交付版本化任务集 `patchtrail-python-v1` 和 `evaluate.py`。评估入口与网页独立，不改变当前 UI；不会自动安装依赖或拉取镜像。`validate` 不请求模型，`run` 在显式开启后请求真实模型。

本轮实际结果和保留的环境失败见 [验证记录](evidence/evaluation-v1-2026-10-17.md)。

后续已恢复 Docker 并完成一次真实 DeepSeek 基线：7 个修复任务通过、1 个无效基准正确拒绝，28 次模型请求。详细结果、工具失败和限制见 [真实评估记录](evidence/deepseek-baseline-2026-10-17.md)。

当前默认 Agent 已改用 [结构化编辑协议](STRUCTURED_EDITING.md)，新运行标记 exact_replace_v1；上述基线仍属于旧手写 diff 协议。后续独立完成新协议真实对照：同样 7/7 修复通过，模型请求 28 → 21 次、工具错误 7 → 0 次、总 token 50,885 → 38,391，耗时基本持平。两轮结果分别保存，详细限制见 [对照记录](evidence/deepseek-structured-comparison-2026-10-17.md)。

## 1. 范围与解释边界

这是 8 个自编的小型 Python 样例：7 个可修复任务、1 个故意无效的回归基准对照。每个任务具有自然语言需求、固定源码、受保护的回归/反馈/保留测试；修复任务另有供校验使用的参考补丁。仅使用标准库业务代码和现有 pytest 镜像。

任务集用于回归、暴露机制问题和积累真实模型基线；它不是 SWE-bench，不代表企业仓库分布，也不能得出通用代码修复成功率。任务和保留测试公开存放于本仓库，所以这里的“保留”是运行时不进入模型上下文，不是从未公开的数据。不能据此排除训练数据污染或后续人工调参影响。

| ID | 目标 | 关键检查 |
| --- | --- | --- |
| refund-discount | 退款扣除折扣 | 无折扣、全额折扣、小数；另有迎合反馈测试的错误补丁 |
| page-ceiling | 分页向上取整 | 空记录、整页、尾页、每页一条 |
| average-empty | 空列表平均数 | 空返回 None，非空和为零仍返回 0 |
| stable-unique | 稳定去重 | 首次出现顺序、负数、不修改原列表 |
| boolean-text | 布尔字符串解析 | 大小写/空白、真假值、非法值抛异常 |
| checkout-discount | 跨文件折扣传递 | helper 实现和调用方都需修改，复用 helper，只加一次运费 |
| cache-none | 缓存命中判断 | None/0/False 都是有效缓存；loader 仅在缺键时调用 |
| invalid-regression | 前置拒绝对照 | CONFIG_ERROR / baseline_regression_failed，零模型调用 |

定义见 [cases.json](../benchmarks/v1/cases.json)。需求不依赖只在保留测试中披露的新规则。跨文件任务的结构性要求通过替换 helper 验证调用行为，没有要求模型必须生成与参考补丁相同的文本。

## 2. 四个命令

以下均在项目根目录用 PowerShell 执行。默认输出至新的 `.patchtrail/evaluations/eval-<uuid>/`，已被 Git 忽略。显式输出路径必须不存在；程序拒绝覆盖旧实验，不自动恢复、不混合多次结果。

列出任务，无 Docker、模型或密钥要求：

```powershell
.\.venv\Scripts\python.exe evaluate.py list
```

生成可独立运行的 TaskSpec 和 Git 仓库，不执行其中的 Python：

```powershell
.\.venv\Scripts\python.exe evaluate.py prepare --output .patchtrail/evaluations/prepared-v1
```

每个任务生成 `repository/`、`task.json`、`checks/`、适用时的 `corrected.patch` / `overfit.patch` 和 `fixture-manifest.json`。源码提交使用固定身份和时间，不继承外部 Git hooks、全局配置或签名设置；相同源码和任务 ID 的提交哈希可重复生成。路径随本机输出位置变化，TaskSpec 因此使用本机绝对路径。

校验任务定义，运行真实 Docker，不调用模型：

```powershell
.\.venv\Scripts\python.exe evaluate.py validate --output .patchtrail/evaluations/validation-v1
```

要求 Docker 可用、已有 `patchtrail-pytest:0.1`；通过 `--image` 可指定其他本地已有镜像。继承独立验证器的镜像固定和容器策略，不下载镜像、不自动安装依赖，不在宿主执行样例 Python。

校验内容：

1. 修复任务：原始回归通过、反馈测试复现失败。
2. 参考补丁：通过完整独立验收，包括保留测试。
3. 退款任务的 overfit 补丁：通过回归与反馈，必须以 holdout_failed 结束。
4. 无效基准对照：必须以指定配置错误结束。

`VALID` 只表示样例契约已校验，不表示模型修复成功。任何样例失败保留验证引用；基础设施/系统错误停止后续运行。完整输出仍保留 NOT_RUN 行。

真实模型评估需要显式启用付费调用，并指定整个实验的调用上限：

```powershell
.\.venv\Scripts\python.exe evaluate.py run `
  --case refund-discount --case checkout-discount `
  --output .patchtrail/evaluations/deepseek-baseline-v1 `
  --allow-model-calls --max-total-model-calls 16 `
  --max-model-calls 8 --max-candidates 2
```

**以上命令会请求本机 `.env` 配置的真实服务并可能产生费用，执行前需明确预算。** `--case` 可重复指定，执行顺序始终遵循固定任务集顺序；未知/重复 ID 会拒绝。省略 `--case` 选择全部任务。建议先校验全套样例，再从两个任务开始建立小预算基线。

`--env-file` 可指定显式本机模型配置；默认只读项目根目录 `.env`。没有模拟模型 CLI 选项或失败兜底。真实模式要求真实适配器及 real_model 结果标记，测试代码使用的 mock 不构成真实运行证据。

退出码：成功为 0；实验中有失败、取消或未执行任务为 1；参数、配置、输出路径等入口错误为 2。真实实验中正确的前置拒绝对照不算实验失败。

## 3. 预算和中断

每任务默认最多 8 次模型请求、2 个候选；工具数、上下文和活跃时间沿用 AgentBudget，实际值完整记入报告。整个实验顺序执行，实际分配为 `min(每任务额度, 剩余额度)`。正常返回后只扣除实际请求数，包括失败请求；异常或中断导致计数未知时不归还已分配额度，立即停止后续任务。

预算按请求次数计，不是金额保证。模型输出上限、超时和关闭传输重试的信息保存在公开模型配置中。Agent 的活跃时间检查仍是节点边界检查，不是随时强杀的全局截止时间。

每个案例结束原子更新 report.json，并生成 report.md。运行前写入 RUNNING 和预留额度；程序被强杀时可看到最后已落盘状态，但这不等于可恢复执行。原子替换不承诺断电后的磁盘持久性。已有目录必须保留，重新运行使用新目录。

## 4. 指标及证据

- 修复成功率：独立验收成功的修复任务数 / 本次选择的全部修复任务数。失败和 NOT_RUN 不从分母中删除。未选择任务不在分母。
- 对照结果：单独统计准确状态、原因和零模型调用的前置拒绝，不加入修复成功分子或分母。
- 参考校验模式：repair_success_rate 与 repaired_cases 为 null，不提供模型成功率。
- 每案例记录状态、原因、结果类别、模型/工具/候选次数、耗时和验证/Agent 产物位置。多候选后成功单独计数；多个工具调用不等同于测试失败后重试。
- token：累加提供商返回的完整已知 usage；缺失不填零，而是保留 unknown_usage_calls 和 partial 状态。未知调用数量另外用 unaccounted_reserved_calls 表示，model_calls 为已知计数下界。
- 费用始终保留 cost_usd=null、cost_status=unknown，尚未实现按价格版本核算。

不能只凭 Agent 声称 PASSED 计分。成功审计会读取最终独立验证结果，核对完整保留验收、关键文件 manifest、基准提交、候选树和补丁哈希及各候选测试阶段。它是针对受控代码的证据一致性检查，不是抵御恶意同进程 Python 的安全证明。

每次实验记录原始数据文件 SHA-256、数据快照及其哈希、全部选中任务、源码基准、Python/平台/依赖版本和实现文件哈希。实际镜像 ID 和测试快照在独立验证证据中。任务完成后检查源码和测试库存/内容未变化；参考补丁和保留套件在 Agent 可读仓库之外，不传入工具或模型上下文。

输出包含私有代码和执行证据，默认不提交。对外分享应另写脱敏摘要。摘要必须区分参考补丁验证、脚本测试与真实模型运行，保留失败记录，不把人工调整后的重跑算作零干预成功。

## 5. 测试与后续

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_evaluation.py -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

新增自动化检查覆盖全部参考补丁重放、确定性 Git 提交、任务选择、样例篡改、独立验收证据篡改、未知使用量、分母计算、实验总预算、异常脱敏及付费入口开关。`evaluate.py validate` 是另一层真实容器校验，不替代这些测试。

已有一次固定配置真实模型基线，后续应保留原始结果，再比较编辑协议、定位工具或提示改动，并增加更有区分度的任务。当前不支持自动重复试验、并发评估、跨实验 resume 或统计置信区间。不要仅依据这 8 个公开小样例宣布可靠性提升。

## 6. 本机临时目录空间不足

本次回归曾遇到 C 盘空间耗尽，导致系统临时目录写入和 Git 初始化失败；另一次容器校验出现 Docker 清理超时。这些失败日志保留，不能统计为修复失败或测试通过。

如果 D 盘空间充足，可仅为当前 PowerShell 进程指定项目内的临时目录后重跑：

```powershell
$taskTempDir = Join-Path (Get-Location) '.test-artifacts\evaluation-temp'
New-Item -ItemType Directory -Path $taskTempDir -Force | Out-Null
$env:TEMP = $taskTempDir
$env:TMP = $taskTempDir
```

这不修改系统级配置、不移动或清理已有文件。切换宿主临时目录不能解决 Docker 自身存储盘不足；真实容器评估前仍需确认 Docker 可用。环境异常会记录为 ENVIRONMENT_ERROR，并停止后续案例，不按模型能力失败归因。
