# DeepSeek 固定任务集真实评估基线

日期：2026-10-17。本轮真实评估由用户明确要求执行；另行授权重启了无响应的 Docker Desktop。实际请求使用本机密钥，本文不包含密钥或模型私有会话。

## 结论

固定任务集中的 **7 个修复任务全部通过最终独立验收，1 个无效基准对照在零模型请求下正确拒绝**。本轮所有任务都执行完毕，没有剔除失败或未执行任务，没有人工修改候选后重跑。中途没有修改模型提示、实现文件或任务集。

这只是 7 个公开、自编的小型 Python 修复任务的单次结果，不是通用仓库成功率，也不是 SWE-bench 成绩。三个任务触发了补丁工具错误后的自动纠正；没有发生已提交候选测试失败后的第二候选重试，因此不能根据本轮证明测试驱动纠错的收益。

## 固定配置与环境恢复

- 数据集：`patchtrail-python-v1`，7 个 repair + 1 个 preflight_reject。
- 原始数据文件 SHA-256：`bbda61692b0d259f95b748604d8a18fe970a951506d949bde42542ee6a62035f`。
- 配置模型：`deepseek-flash`，地址 `https://api.deepseek.com`，关闭思考模式；没有记录提供商内部模型版本。
- 每任务最多 8 次模型请求、2 个候选；实验最多 48 次模型请求。
- 每次输出上限 4096 token，模型请求超时 60 秒，传输重试为 0。
- 镜像：`patchtrail-pytest:0.1`，实际 ID 为 `sha256:430a1d9d69c9b4def247505fef9b5e4137fac95ac843ace6e5bff6729cec8179`。
- 实现、依赖、平台、配置和数据快照随实验记录；执行后核对实现身份与启动记录一致。

磁盘清理后 Docker 仍无响应，前置校验首先返回 ENVIRONMENT_ERROR；该阶段没有模型请求。常规重启未能结束卡住的进程，获得用户授权后重启 Docker 自身进程和 `docker-desktop` 专用 WSL 实例，未终止其他 WSL 发行版。清理了能确认归属于旧验证记录的一个已停止容器。

恢复后，用新目录完整执行 validate，8 / 8 VALID。确认原始 Bug 可复现、参考补丁通过完整验收、退款 overfit 补丁被保留测试拒绝，再执行下面唯一一次真实模型批量评估。重启前失败报告和前一阶段的磁盘/容器失败均保留，不混入模型能力统计。

```powershell
.\.venv\Scripts\python.exe evaluate.py run `
  --output .patchtrail/evaluations/deepseek-baseline-20261017-a `
  --allow-model-calls --max-total-model-calls 48 `
  --max-model-calls 8 --max-candidates 2
```

## 逐任务结果

| 任务 | 最终状态 | 模型请求 | 验证候选 | 补丁工具失败 | token 总数 | 全任务耗时/秒 |
| --- | --- | --- | --- | --- | --- | --- |
| refund-discount | PASSED | 3 | 1 | 0 | 4,623 | 47.734 |
| page-ceiling | PASSED | 3 | 1 | 0 | 4,786 | 36.312 |
| average-empty | PASSED | 3 | 1 | 0 | 4,860 | 35.313 |
| stable-unique | PASSED | 6 | 1 | 3 | 11,735 | 40.000 |
| boolean-text | PASSED | 6 | 1 | 3 | 12,421 | 39.531 |
| checkout-discount | PASSED | 3 | 1 | 0 | 5,383 | 34.735 |
| cache-none | PASSED | 4 | 1 | 1 | 7,077 | 35.766 |
| invalid-regression | CONFIG_ERROR / baseline_regression_failed | 0 | 0 | 0 | 0 | 4.593 |

每个修复任务的最终回归、反馈、保留测试均通过。无效基准的 CONFIG_ERROR 是正确拒绝，不算修复成功，也不加入修复成功率分母。

汇总：28 次模型请求、30 次工具调用、7 个进入验证的候选；输入 47,189 token，输出 3,696 token，总计 **50,885 token**。全部请求 usage 已知，没有用零补齐未知使用量。案例耗时合计 273.984 秒，8 案例中位耗时 36.039 秒；包括源码准备和容器测试，不是纯模型延迟，不包含前面的环境恢复与全量样例校验。费用未核算，`cost_usd=null`、`cost_status=unknown`。

## 实际暴露的问题

stable-unique 和 boolean-text 各有 3 次 propose_patch 失败，cache-none 有 1 次，共 7 次，全部为 `patch_does_not_apply`。原始错误和失败补丁保留在 tool-events 和 proposals 中，不因最终通过而抹去。

检查首个失败补丁可见 hunk 行数与实际内容不一致：例如 stable-unique 标为 `+1,9`，实际新文件只有 8 行；cache-none 原文件有 6 行，却使用 `-1,5`。工具返回 Git 的 corrupt patch 错误，模型在既定预算内自行修正。最终 stable-unique 和 boolean-text 补丁还使用了合法的无末尾换行标记；功能通过，但格式风格仍有改善空间。

因此当前直接可见的问题是让模型手写 Git diff 行数带来的交互成本。后续可以比较结构化编辑、由工具构造 diff 或更准确的格式诊断；这只是后续实验方向，本轮没有改变补丁解析规则，也没有自动修正模型提交内容来提高成绩。

对最终补丁进行了逐例人工式阅读核对：退款扣除折扣、分页向上取整、空列表返回 None、去重保持顺序、布尔文本规范化与异常、跨文件复用折扣 helper、缓存通过键是否存在判断命中，均与当前任务要求一致。average-empty 还额外复制了输入列表，虽不违反当前契约，但增加 O(n) 辅助内存；通过测试不代表补丁已经最简或经过性能优化。

## 证据审计与本机产物

- 再次读取 7 个最终独立验收结果，核对完整保留验收、阶段结果、基准提交、候选树及补丁哈希。
- 核对 Agent 和每次独立验证 manifest 中的 **1,054 个文件条目**，全部一致。
- 核对原始样例源码/可信测试未变化；模型会话中不包含 corrected.patch 或 holdout_dir 字段。
- 对本轮实验的 **1,463 个文件**扫描本机密钥值，未发现凭证泄露。此扫描不等于所有隐私信息都可公开。
- 执行后按 PatchTrail 标签检查，没有遗留容器。

本机原始产物被 Git 忽略：

| 路径 | 用途 |
| --- | --- |
| `.patchtrail/evaluations/recovered-preflight-20261017-a/report.json` | Docker 恢复前的前置环境失败，零模型调用 |
| `.patchtrail/evaluations/recovered-validation-20261017-b/report.json` | Docker 恢复后的完整 8 项参考校验 |
| `.patchtrail/evaluations/deepseek-baseline-20261017-a/report.json` | 本轮唯一一次真实模型批量评估汇总 |
| `.patchtrail/evaluations/deepseek-baseline-20261017-a/report.md` | 可读汇总 |
| `.patchtrail/evaluations/deepseek-baseline-20261017-a/<case-id>/agent-runs/` | 候选、工具失败、私有消息与独立验证引用 |
| `.test-artifacts/deepseek-baseline-20261017-a-audit.json` | 补充证据审计、工具错误与使用量明细 |

对外可分享本文和版本化任务定义；不应直接上传包含模型私有会话的整个运行目录。下一轮评估应使用新目录，并保持本轮记录不变。
