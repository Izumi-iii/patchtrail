# LangGraph 检查点与模型执行恢复

日期：2026-10-18。面向本地、受控 Python 仓库；不接入网页，不新增依赖，不自动调用真实模型做评估。

## 交付范围

每个 Agent 运行使用固定 `thread_id=agent-run-id`，在私有运行目录保存 `checkpoints.sqlite3`。图以 `durability="sync"` 执行，恢复时向同一线程传入 `None`，从最新检查点继续。禁止指定历史 checkpoint 分叉，禁止恢复时提高原预算。

官方的 [持久化说明](https://docs.langchain.com/oss/python/langgraph/persistence) 区分图检查点和普通数据存储；检查点不会自动还原 Python 对象或磁盘。此实现补充保存运行胶囊、节点回执和模型请求预约，不把进度日志当成恢复机制。

不安装额外 SQLite 插件：`LocalCheckpointSaver` 实现现有 `BaseCheckpointSaver` 的同步接口，使用标准库 SQLite、现有 LangGraph 严格序列化器（关闭 pickle 回退）、完整检查点及 pending writes。它只为当前有界、串行 StateGraph 提供同步存储；不声称支持异步图、DeltaChannel、远程数据库或多租户生产服务。

## 恢复的信息和校验

- 图状态：完整消息、待执行工具调用、当前响应、状态和分支。
- 运行状态：累计模型/工具/候选数、usage（未知项保留 null）、已使用工具 ID、已提交候选哈希、反馈结果及最终验证引用。
- 文件状态：固定基准树、当前候选目录与文件清单、累计补丁哈希、submitted 状态、提案序号、冻结测试树哈希、验证清单哈希。
- 身份：原 TaskSpec、运行绝对路径、公共模型配置、脚本/真实模型标记、原预算、关键实现文件及依赖版本。模型配置的 API 密钥不写入检查点，恢复时从本机环境重新读取。

恢复前校验任务、实现、依赖与公共模型配置未变，输入仓库 HEAD 和干净状态不变，基准/候选、补丁、冻结测试、已完成的验证证据与记录一致，固定镜像仍可用。变化时停止，不自动重置仓库或接受新基准。运行目录不能随意移动；旧版没有数据库的运行不能恢复，也不作推测性迁移。

SQLite 数据行附 SHA-256，检测意外损坏；这不是签名，不能抵抗可同时修改数据库及产物的本机管理员。运行目录是私有控制侧数据，不能从被审查仓库导入数据库。图检查点、节点回执含源码和消息，应与原运行一起私有保存。

## 节点副作用与崩溃窗口

| 退出位置 | 默认恢复行为 |
| --- | --- |
| 节点输出已有持久化回执，图检查点尚未推进 | 返回原输出并恢复运行状态，不重新请求模型或重复发布同一逻辑工具结果 |
| 准备节点未完成 | 将本运行的部分 base/trusted-suites/proposals 目录移至 abandoned，再准备；不触碰输入仓库 |
| 工具节点执行中 | 从节点输入状态重放该批本地工具；已发布但未记回执的候选留作孤立产物，新提案使用新序号 |
| 反馈或最终验证中 | 清理经 label 和 inspect 双重确认归属本验证运行的遗留容器，再重新验证；不把半份报告当成通过 |
| 最终验收已完成，终态图检查点尚未导出 result | 从终态检查点补全导出，不重新进入模型 |
| 已完整结束 | 返回原结果，不重新运行图、模型或改写原证据清单 |

回执在节点完成后、返回图之前持久化。工具和验证不是物理 exactly-once：硬退出窗口内可能重放私有候选编辑、读取及 pytest。`tool_calls` 记录恢复后图中的逻辑调用数；物理重放可从重复 node_started、node_replayed 事件及孤立提案/验证目录核对。验证重放不增加逻辑候选数。失败候选的历史及 seen-candidates 集合保持，不重置预算。

完整结束标记最后写入：先导出 result、消息和事件，再封存 manifest，最后提交 SQLite 生命周期完成标记。中途退出时终态图仍可补全导出；不会仅凭“完成”标记返回缺失清单的产物。验证完成后进入人工审批，仍由审批模块核对原验证证据。

模型调用前先持久化预约并消耗一次预算，响应与节点输出保存完成后才可无请求恢复。**网络请求已发出但响应未持久化时，本地无法证明服务端是否完成或计费**；默认返回 `RECOVERY_REQUIRED / model_outcome_unknown`，不自动重发。显式允许重试后，新请求另占一次原预算，旧请求 usage 保持 null，仍可能再次计费。没有宣称模型请求 exactly-once，也不伪造提供商幂等能力。

已保存响应的 usage、工具 ID 和调用计数均复用。活动时间累计已落盘节点边界的时间，正常停机间隔不计入；硬退出可能丢失最后一个未落盘节点的耗时，因此仍是软活跃时间预算，不是跨进程精确计费计时。模型/容器保留各自超时，模型请求次数上限在发出前落盘。

## CLI

新运行默认启用检查点：

```powershell
.\.venv\Scripts\python.exe run_agent.py --task .patchtrail/demo/task.json --max-model-calls 8 --max-candidates 2
```

按 Ctrl+C 返回 `SUSPENDED`；操作系统硬终止可能来不及更新 result.json，此时保留 RUNNING 不代表仍有进程。每个运行使用 OS 文件锁协调执行，进程退出后锁自动释放；两个进程不能并发恢复同一运行。

```powershell
.\.venv\Scripts\python.exe run_agent.py --resume '.patchtrail/agent-runs/agent-替换为运行ID'
```

恢复入口仍会根据后续节点调用配置的模型，可能产生费用。不提供新的候选/模型调用上限参数，使用原预算。遇到结果未知的请求，确认愿意承担额外请求后才显式运行：

```powershell
.\.venv\Scripts\python.exe run_agent.py --resume '.patchtrail/agent-runs/agent-替换为运行ID' --retry-unknown-model-call
```

只有 `SUSPENDED`、`RECOVERY_REQUIRED` 或硬退出的未完成图支持继续；最终业务失败、预算耗尽、策略拒绝都是终态。环境或产物校验失败会拒绝进入图；先恢复一致的环境/原产物再尝试，不能用 retry 标志绕过身份和内容校验。

可在恢复命令后加 `--request-approval`，最终独立验收 PASSED 后创建审批；它不批准或应用补丁。新审批返回 WAITING_APPROVAL；重复请求已有审批时返回其实际状态，例如 APPROVED/APPLIED/REJECTED，不重置人工决定。CLI 成功退出表示读取/创建审批记录成功，不表示补丁已应用。保留集仍只在最终验收运行，失败直接结束，绝不回到模型。

`result.json` 标记 `crash_resume_supported=true`、`recovery_protocol=local-checkpoint-v1` 和是否 `resume_available`。数据库及执行锁是可变的私有恢复数据，不纳入最终 Agent manifest；原消息、事件、工具回执和验证引用仍纳入证据清单。已完成运行反复 resume 不改变这些已封存文件。

## 验证方法

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_checkpoint.py -v
```

恢复专项使用真实子进程 `os._exit` 和 SQLite/文件系统，以无内部记忆的脚本模型和测试验证器检查跨进程恢复；不会把这些状态路由测试算作真实模型评估。覆盖模型回执与图检查点之间的窗口、未知请求、预算保持、部分候选、反馈重试、最终验收、Ctrl+C、身份/测试/数据库变更、互斥及终态幂等。真实 Docker 回归另行开启；不使用新付费模型请求来替代故障注入。

2026-10-18 本机验收：开启真实 Docker 和本机缺 pytest 镜像对照后，完整 **127 项测试全部通过，无跳过**，用时 613.136 秒；其中恢复及存储契约新增 24 项。日志为本机 `.test-artifacts/checkpoint-full-regression.log`（不入 Git）。真实 Docker 恢复用例在 candidate_feedback 创建容器后 `os._exit(78)`，由新进程核对归属、清理遗留容器并重新验收通过；模型仍是脚本模型。

另对最后调整的 CLI 审批状态复用执行定向集成测试，通过；日志为 `.test-artifacts/checkpoint-cli-integration.log`。它验证先恢复到 WAITING_APPROVAL，再由测试操作者批准后重复 resume，返回 APPROVED、保留原决定、无新模型调用且不应用文件。本轮未发起真实付费模型请求，也未重新计算真实模型成功率。
