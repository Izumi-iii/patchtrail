# 网页与 Agent 后端集成

日期：2026-10-18。参考主流问答产品的左侧会话、主对话区、底部输入框；进度和审批显示在任务回复中，保留 PatchTrail 自己的名称。当前仅为本机受控仓库工作台，不提供公网或多用户部署。

## 直接试用

在仓库根目录启动：

```powershell
.\.venv\Scripts\python.exe app.py --port 8770
```

打开 http://127.0.0.1:8770 。本机 8766 当前由另一应用使用，因此本次服务选择 8770，没有停止其他应用。模型读取项目根目录 `.env`，网页不接收或显示 API 密钥。修复会调用配置的 DeepSeek/OpenAI-compatible 模型，可能产生费用；每个任务最多 8 次模型请求、2 个候选，恢复不增加原预算。

1. 新建会话，保留默认「修复任务」模式和「退款示例」设置。
2. 点击「试试退款修复」填入需求，再发送。每次示例任务新建独立仓库，不修改正式项目。
3. 在回复卡片查看阶段、工具事件、模型请求次数、候选数和最终测试状态。
4. 验收通过后先「查看补丁」，核对目标路径、基准、补丁 SHA 和 diff，再选择批准或拒绝。批准按钮在当前页面查看该 binding 后才启用；后端再次校验 binding。
5. 批准仅记录决定；「应用已批准补丁」才会改动目标文件，不创建 Git 提交或推送。应用中断时提供「恢复补丁应用」。

「记录需求」与「仓库检查」保留原来的只读行为，不调用模型。网页不是通用闲聊助手，修复模型的目标仍是指定测试和修改范围内的 Python 代码。

## 修复自己的仓库

打开「修复设置」，选择本机 TaskSpec JSON 文件的**绝对路径**。格式沿用 [验证器契约](VERIFIER.md) 和现有 `examples/create_demo.py` 样例；必须明确 repository_path、固定 base_commit、image、editable_paths、import_modules、regression_dir、feedback_dir，可选 holdout_dir 等字段。

网页读取并验证 TaskSpec；发送的需求文本替换本次 task.request，其余基准、范围和测试规则不变。任务文件保存后会关联其中的仓库。单独使用「关联仓库」只供仓库检查，不会自动推测可信测试或绕过任务配置。仓库不干净、基准测试无效或环境缺失时，执行准备阶段会拒绝，不能算作修复成功。

模型只收到原有受限源码工具与反馈信息，保留测试依旧只供最终独立验收，失败后不再返回模型。网页展示测试结果不意味着这些内容被添加到后续模型上下文；每个新修复请求使用本次任务文本，不自动传入整段网页历史。

## 任务、暂停与恢复

```text
QUEUED → RUNNING → WAITING_APPROVAL → APPROVED → APPLIED
             ↘ STOP_REQUESTED → SUSPENDED → 显式继续
             ↘ INTERRUPTED / RECOVERY_REQUIRED → 显式恢复
             ↘ FAILED
WAITING_APPROVAL → REJECTED / EXPIRED / INVALIDATED
```

发送消息后，HTTP 只持久化任务并启动独立 Python 子进程。模型与 Docker 验证不占用 HTTP 请求线程。网页约每 1.5 秒轮询真实进度；当前没有逐 token 输出或 SSE，不展示虚构百分比。刷新页面不会重新提交任务，关闭/重启网页服务也不会主动终止后台进程。

「暂停执行」写入控制标志，在下一个图/工具检查边界挂起。正在进行的模型请求或 pytest 会等待当前步骤返回，不能保证立即停止；若最终验收已经结束，会保留真实完成结果。不会通过 PID 杀进程，因此也不会误杀复用 PID 的程序。

同一数据目录使用 SQLite 唯一索引和 OS 控制锁限制为一个活跃修复任务。后台进程另持有每任务 OS 锁。进程退出后锁自动释放；超过启动宽限且无持锁进程时，显示 INTERRUPTED，等待用户恢复，不自动重新调用模型。恢复每次生成新的 attempt 标识，旧进程不能再次启动或覆盖新尝试。

模型请求发出但响应未落盘时，恢复返回 model_outcome_unknown。页面说明结果和费用未知，只在用户点击「明确重试模型请求」后才增加一次请求；原请求继续占用原预算。更多边界见 [检查点恢复](CHECKPOINT_RECOVERY.md)。

## 数据与接口

沿用 `.patchtrail/workbench.sqlite3`，新增 jobs 表和会话 task_source/task_path 字段，保留原会话与消息。任务消息、任务记录和请求去重在同一事务提交，刷新、网络重试不产生重复模型任务。活动任务期间禁止删除会话或更改任务设置。原始运行记录保留在本机，删除终态会话不会递归删除代码或执行产物。

私有数据布局：`web-jobs/<job-id>/task.json`、fixture、worker.log、pause.requested；`r/<短槽位>/agent-.../` 保存 Agent 与验证证据，缩短 Windows 嵌套路径；`approvals/` 保存审批。槽位创建拒绝覆盖已有目录。数据目录仍不宜放在过深的路径下；所有这些目录均在 Git 忽略的 `.patchtrail` 下。

| 接口 | 用途 |
| --- | --- |
| GET /api/healthz | 能力、公共模型名与本机配置是否存在，不返回密钥 |
| POST /api/conversations/{id}/task | 设置示例或本机任务文件 |
| POST /api/conversations/{id}/messages，mode=repair | 持久化并提交任务；原 inspect/note 模式不变 |
| GET /api/conversations/{id}/jobs | 会话任务与公开进度 |
| GET /api/jobs/{id} | 状态、计数、有限事件和最终验证摘要 |
| GET /api/jobs/{id}/diff | 已通过验收的冻结补丁及 binding |
| POST /api/jobs/{id}/actions | pause/resume/retry_unknown/approve/reject/apply/recover_apply |

复用同源、Host、X-PatchTrail 检查。请求不能传入自由 shell、模型密钥或其他任务的 approval_id；审批从服务端任务关联读取。消息、仓库文本和 diff 均通过 textContent 展示。公开接口不暴露 messages.private.json、模型工具参数、完整测试日志或任意文件下载。

## 验证与限制

新增测试使用真实 Agent 图和临时 Git 仓库：脚本模型不作为生产兜底；真实 Docker 用例验证网页任务确实进入独立验收。浏览器专项启动独立测试服务，以脚本模型验证提交、暂停、重启服务、恢复、diff、批准、应用、刷新去重与手机布局。生产 app.py 和 web_worker.py 不加载测试模型。

既有 CLI 运行没有自动导入网页；这里管理的是网页创建的任务。网页仍基于标准库本地 HTTP 服务，当前没有账户、远程仓库拉取、任意 shell、模型配置编辑或公网认证，不宣称生产多租户服务。

### 本机验收记录

2026-10-18：完整回归 **141 项通过，无跳过**，用时 754.393 秒，日志 `.test-artifacts/web-connected-full.log`。最后补充正式启动路径和 Git 环境隔离后，网页任务模块 **14 项复测全部通过**，日志 `.test-artifacts/web-jobs-final.log`；这包含后来新增的两个测试，不把它们算进先前 141 项完整回归。

两组真实 Chromium 检查共 **21 项通过**：原会话/只读行为 15 项，Agent 完整交互 6 项。截图与检查记录保存在 `.test-artifacts/webui/` 和 `.test-artifacts/web-agent/`。Agent 浏览器流程使用脚本模型，完成暂停、重启服务、恢复、查看冻结 diff、批准、独立应用、刷新去重和手机无横向溢出检查。

正式启动路径另用实际 web_worker.py、LangChain HTTP 适配器、本机 OpenAI-compatible 假服务和真实 Docker 完成独立验收，3 次本机测试请求；并未请求真实 DeepSeek，也不作为真实模型能力评估。生产页面实际健康检查显示 deepseek-flash 已配置；本轮没有新增付费模型请求。

复测入口（需已有环境，不自动安装）：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_web_jobs.py -v
node tests/webui/agent.browser.cjs
node tests/webui/workbench.browser.cjs
```

真实 Docker 用例需显式设置 `PATCHTRAIL_DOCKER_TESTS=1`；浏览器复用已有 Playwright/Chromium，可通过 `PATCHTRAIL_PLAYWRIGHT_MODULE`、`PATCHTRAIL_BROWSER_PATH` 指定本机安装位置。
