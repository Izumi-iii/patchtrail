# 固定任务集与评估入口验证记录

日期：2026-10-17。数据集：patchtrail-python-v1。

后续进展：用户清理磁盘并授权重启 Docker 后，完整参考校验已恢复至 8/8 VALID，并完成真实模型批量评估。见 [后续真实基线](deepseek-baseline-2026-10-17.md)。下文保留本阶段当时的失败和未验证状态，不回写历史结果。

数据定义文件 SHA-256：`bbda61692b0d259f95b748604d8a18fe970a951506d949bde42542ee6a62035f`。

本轮没有调用真实模型，没有消耗 DeepSeek 请求预算。下面的参考补丁结果不能计为模型修复成功率。

## 已完成的验证

1. 首轮完整 Docker 样例校验：8 / 8 VALID。7 个修复任务的基准均满足回归通过、反馈复现失败，参考补丁均通过完整保留验收；无效基准对照得到预期配置错误。
2. 退款 overfit 补丁通过回归和反馈，但以 REPAIR_FAILED / holdout_failed 结束。共核对 16 份独立验证记录，包括基准、参考补丁和 overfit 检查。
3. 最终自动化回归：运行 64 项，57 项通过、7 项明确跳过，零失败。7 个跳过项是未启用的原有 Docker 集成用例；上面的样例容器校验是单独执行的，不将跳过项算作通过。新增的 15 项评估测试全部通过。
4. CLI prepare 实际生成了跨文件任务；拒绝已有输出目录、无效总预算、未知任务 ID 和未显式启用的模型请求入口。
5. Python 编译、Git 差异、UTF-8/Markdown、文档文件链接和真实凭证内容扫描通过。源码和文档中没有写入本机密钥。

本机证据（被 Git 忽略）：

| 位置 | 内容 |
| --- | --- |
| `.patchtrail/evaluations/fixture-v1-validation/report.json` | 首轮全量 8 项真实 Docker 校验；使用早期报告格式 |
| `.patchtrail/evaluations/fixture-v1-validation/<case-id>/verification-runs/` | 原始容器、结构化报告、补丁和 manifest |
| `.test-artifacts/evaluation-tests-d-drive.log` | 最终 64 项回归，93.325 秒，7 项跳过 |
| `.patchtrail/evaluations/prepare-cli-check/` | prepare 入口实际生成的跨文件样例 |

## 保留的失败及环境限制

首轮完整校验之后，报告增加了实现身份记录、未知调用预算统计和基础设施错误归类。重跑最终入口时，5 项 VALID，checkout-discount 遇到 Docker 容器清理超时，后续 2 项 NOT_RUN；该实验以 ENVIRONMENT_ERROR / docker_unavailable 停止。结果保存在 `.patchtrail/evaluations/fixture-v1-final/report.json`，不覆盖首轮证据。

一次完整 unittest 回归在系统临时目录所在 C 盘零剩余空间时出现 2 个错误：临时文件写入失败、样例仓库 Git add 失败。日志保留在 `.test-artifacts/evaluation-tests-final.log`。仅为重跑进程设置 D 盘项目临时目录后，同一完整回归通过，结果见上表。

为剩余 3 个案例单独重试的容器校验仍遇到 Docker 命令超时，结果保存在 `.patchtrail/evaluations/fixture-v1-d-drive/report.json`：1 项 ENVIRONMENT_ERROR、2 项 NOT_RUN。没有重写这些失败记录，也没有把它们按任务修复失败统计。切换宿主临时目录没有恢复 Docker 自身的可用性。

因此：任务集已有完整容器校验证据，最终非容器自动化回归通过；但当前机器的 Docker 环境仍需恢复后复查，不能声称最终版本又完成了一次全量容器校验，或容器资源全部已确认回收。下一次真实模型评估前，应先恢复可用磁盘空间、检查 Docker 和本项目残留资源，再使用新输出目录执行 validate。

## 尚未验证

尚未使用该任务集运行真实 DeepSeek 批量评估，未得到真实模型成功率、真实失败后重试率或费用结果。单次公开小样例集合不能推断通用仓库可靠性；也未实现评估进程崩溃恢复或自动跨实验合并。
