# Agent 运行闭环：LangChain、LangGraph 与 DeepSeek

日期：2026-10-16。现有 Web UI 暂不调整；本阶段通过独立入口运行后端。

真实运行证据：已使用 DeepSeek 完成一个退款样例，两次执行共 5 次模型请求，包含首次补丁格式失败与后续成功。完整限制与使用量见 [小样例记录](evidence/deepseek-smoke-2026-10-16.md)，不据此声称通用修复成功率。

2026-10-17 新增固定任务集真实基线：7 个修复任务通过最终独立验收，1 个无效基准零模型调用正确拒绝，合计 28 次模型请求、50,885 token。三个任务发生补丁工具错误并自行纠正，没有出现第二候选测试重试。详见 [完整评估记录](evidence/deepseek-baseline-2026-10-17.md)，不外推通用仓库成功率。

同日新协议独立对照：修复和拒绝结果保持不变，模型请求降为 21 次、总 token 38,391、工具错误 0 次；耗时基本持平。详见 [结构化编辑对照](evidence/deepseek-structured-comparison-2026-10-17.md)。这是两个版本各一次的小样例观察，不是通用节省比例或费用估算。

## 1. 实现范围

已经接入：

- LangChain ChatOpenAI 的标准工具调用接口，对接 DeepSeek 的 OpenAI-compatible Chat Completions API。
- LangGraph 的显式节点与分支：准备 → 模型 → 工具 → 可反馈验证 → 有限重试 → 最终独立验收。
- 代码目录、字面检索、按行读取、Python AST 定义查询。
- 完整累计补丁提交、过期内容哈希检查、源码修改范围限制、候选冻结。
- 在调用模型前执行环境和基准检查，失败时不花费模型调用额度。
- 固定 commit、固定镜像 ID、冻结可信测试副本；复用已有独立验证器。
- 模型/工具/候选数、上下文字符及活跃时间预算，记录真实使用量或未知值。
- 本地私有会话记录、工具调用记录、事件、图拓扑、候选与验证引用。

2026-10-18 已新增 [本地人工审批](APPROVALS.md)：最终验证通过后冻结产物，独立 CLI 批准、拒绝及应用；审批决定和应用操作支持持久化及跨进程恢复。`run_agent.py --request-approval` 创建待审批记录，不直接修改目标仓库；原 Agent 的 PASSED 仍只代表验证通过。

同日新增 [LangGraph 检查点恢复](CHECKPOINT_RECOVERY.md)：新运行默认以 SQLite 保存图状态、运行胶囊及节点回执，使用 `run_agent.py --resume RUN_DIRECTORY` 跨进程继续。恢复候选、可信测试与预算，校验内容及配置；结果未知的模型请求默认停止，只有显式允许才重新请求并额外计数。`crash_resume_supported=true` 描述新协议的有边界恢复能力；旧运行没有数据库则不能恢复。

同日网页已接入执行、进度、暂停恢复、补丁审批及应用，见 [网页操作说明](WEB_AGENT.md)。

未实现：自动模型传输重试、逐 token 网页输出、异步/分布式检查点、多租户和通用 shell。私有工具/验证在未记回执的退出窗口内可重放；不宣称外部副作用 exactly-once。

## 2. 项目环境

根据用户授权，已在项目 `.venv` 使用 Python 3.12.2 安装并验证导入：

| 包 | 本次解析版本 |
| --- | --- |
| langchain | 1.4.6 |
| langchain-core | 1.6.11 |
| langchain-openai | 1.7.2 |
| langgraph | 1.2.15 |

完整依赖记录在 [requirements-agent.lock.txt](../requirements-agent.lock.txt)。这是 Windows / Python 3.12 的解析快照，固定版本但不含 wheel 哈希；其他平台需要独立验证，不把它当作跨平台完整环境证明。

恢复相同依赖版本可使用项目虚拟环境：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-agent.lock.txt
.\.venv\Scripts\python.exe -m pip check
```

重新创建或安装环境属于准备步骤，Agent 不会在运行时自动安装依赖。原 `python app.py` 和 `python verify.py` 的标准库入口保持可用。

## 3. DeepSeek 配置

复制 `.env.example` 为项目根目录 `.env`，只在本机编辑：

```dotenv
DEEPSEEK_API_KEY=在本机填写真实密钥
PATCHTRAIL_MODEL=deepseek-flash
PATCHTRAIL_BASE_URL=https://api.deepseek.com
```

这是说明，不能将真实密钥提交到模板、文档或 Git。`.env` 被忽略，`.env.example` 必须始终为空密钥模板。

CLI 默认只读取 PatchTrail 自身的根目录 `.env`，不扫描目标仓库或其他项目的配置。可以通过 `--env-file` 显式指定本机文件。解析不执行命令、不插值变量、不支持多行值；只接受上述字段和可选 PATCHTRAIL_API_KEY。环境变量优先于文件中的同名值，PATCHTRAIL_API_KEY 优先于 DEEPSEEK_API_KEY。

密钥不进入 public 模型配置、运行结果、工具参数或容器。提供商异常仅记录类别，不原样输出可能含凭证的请求。自动 LangSmith tracing 在调用范围内关闭，避免继承全局设置而将源码发送到第二个服务。

### 模型与思考模式

本次查阅的 DeepSeek 官方文档列出 deepseek-flash、deepseek-v4-pro，接口地址为 `https://api.deepseek.com`；模型 ID 保持可配置，不承诺服务端别名永远不变。

第一版明确使用 `thinking: {type: disabled}`。官方文档指出，启用思考模式且携带工具时，需要在后续请求中保留 reasoning_content；当前适配器没有实现该协议，因此不把思考模式静默打开，也不保存内部推理内容。

请求输出上限通过 DeepSeek 的 max_tokens 设置，传输自动重试为 0。模型失败会作为真实失败记录，不切换到规则模型或预置补丁。

官方依据：[首次调用](https://api-docs.deepseek.com/)、[工具调用](https://api-docs.deepseek.com/guides/tool_calls/)、[思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)。

## 4. 运行

需要本地 Docker、已准备的 pytest 镜像、有效 TaskSpec 和显式模型配置：

```powershell
.\.venv\Scripts\python.exe run_agent.py `
  --task .patchtrail/verification-demo/task.json `
  --max-model-calls 8 `
  --max-candidates 2
```

示例任务是人工准备的退款 Bug，用于小规模接通与验证；通过一次样例不能推断真实项目成功率。任务生成和验证器契约见 [VERIFIER.md](VERIFIER.md)。

配置缺失时返回 CONFIG_ERROR；没有模型网络调用，也没有脚本模型 fallback。正式 CLI 不提供“脚本模型模拟成功”选项，脚本模型只存在于测试文件中。

## 5. 图与验证分层

```text
prepare：固定环境、源码与测试，验证基准
  → model：LangChain 工具调用
  → tools：校验并执行读代码/补丁工具
      ├─ 未提交候选 → model
      └─ 已提交候选 → feedback
           ├─ 可修复失败且有额度 → model
           ├─ 环境/配置/策略/预算问题 → 结束
           └─ 反馈通过 → final_verification → 结束
```

验证器新增三个 purpose，公开 `verify.py` 默认仍为 full：

| purpose | 返回的成功状态 | 检查 |
| --- | --- | --- |
| baseline | READY | 原有回归通过，目标 Bug 可复现；不访问保留集 |
| feedback | FEEDBACK_PASSED | 基准、候选回归和反馈套件；不访问保留集 |
| full | PASSED | 必需检查全部完成，包括配置了的保留套件 |

READY、FEEDBACK_PASSED 都不能算修复成功。最终验收没有返回模型的边；即使失败也直接结束。完整最终结果与保留测试日志属于私有证据，不加入模型上下文。

当前为保证状态清晰，每次反馈和最终验收会重新建立基准并运行相应检查；会重复消耗容器时间。后续可在证明环境和套件身份一致的条件下缓存基准，本版不为省时间复用开发容器。

## 6. 工具协议

| 工具 | 用途和限制 |
| --- | --- |
| list_files | 只列可读源码/文档路径，返回树哈希与截断标记 |
| search_code | 字面量搜索，受限结果数和片段长度 |
| read_file | 每次最多 200 行及 12,000 字符，校验文件内容哈希 |
| find_symbol | Python 函数/类定义；只作语法级定位，不宣称完整调用图 |
| edit_files | 对当前文件做唯一原文替换；校验工作树/文件哈希，整批发布；工具生成相对原始基准的累计 diff |
| submit_candidate | 提交当前候选哈希，只表示进入验证 |

没有 shell、pip、网络、宿主任意文件读取或提交 Git 的工具。`.env` 等隐藏/非源码文件不进入可读范围。仓库中的 README、注释和其他文本均为不可信数据，不能授予权限。

默认编辑协议为 `exact_replace_v1`：模型提供路径、文件哈希、old_text、new_text，不编写 diff 文件头或行数。后续编辑针对当前候选；工具自动将全部修改转换成相对原始基准的累计补丁，模型无需重述之前的修改。每文件一个替换，原文必须非空且唯一匹配；第一版只编辑已有允许的 Python 文件，不创建、删除或重命名文件。read_file 的 ends_with_newline 可用于判断文件结尾。

详细输入示例、原子性、边界及测试见 [结构化编辑说明](STRUCTURED_EDITING.md)。旧 propose_patch 只在直接 CodeTools 调用显式设置 allow_legacy_patch=True 时可用，不在默认模型工具列表中；人工 verify.py 仍可接收完整 Git 补丁。历史真实基线使用旧协议，不作为新协议效果证明。

非法工具参数返回结构化错误并计入额度；路径越权和保护文件修改触发策略终止。模型输出“已修好”“PASSED”或自称测试通过都不能设置最终状态。

## 7. 预算与费用

默认硬计数上限：12 次模型请求、40 次工具调用、3 个验证候选；同一失败候选再次提交以 no_progress 结束。模型请求失败或状态未知也消耗一次请求额度。每次输出默认最多 4096 token，输入上下文限制为 60,000 字符，超出时明确停止，不静默删除关键历史。

活跃时间默认 600 秒，在图节点和每次工具调用边界检查；模型请求和容器还有各自超时。这不是可在任何瞬间强制终止的作业级硬截止时间，单个正在执行的验证阶段可能跨过活跃时间边界。

使用量按提供商返回的 usage 记录；未知的请求保持 null。没有登记与核对价格版本，因此 cost_usd 为 null、cost_status 为 unknown，不能当成免费或零成本。

## 8. 产物与验证

默认目录 `.patchtrail/agent-runs/<agent-run-id>/`：

- task.json、effective-task.json：原始任务和固定环境/套件配置。
- result.json、progress.json、events.jsonl：结果、当前节点、调用与执行事件。
- messages.private.json、tool-events/：私有模型/工具消息，包含源码和补丁，不公开导出。
- proposals/、changes.patch：候选及最后补丁；失败时的最后补丁不等于可接受补丁。
- verification-runs/：每次独立验证的完整证据。
- graph.mmd、manifest.json：实际图和关键证据文件哈希。

graph.mmd 用于解释程序流程。`checkpoints.sqlite3` 持久化图检查点和运行状态，执行锁保护同一运行；两者不纳入最终证据 manifest。恢复的实际入口、校验与限制见 [恢复说明](CHECKPOINT_RECOVERY.md)。result.json 明确标记 real_model 或 scripted_test，避免把脚本测试计入真实模型评估。

测试命令：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v

$env:PATCHTRAIL_DOCKER_TESTS = '1'
$env:PATCHTRAIL_NO_PYTEST_IMAGE = 'algo-coach-runner:0.1'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

后一命令的缺 pytest 镜像只是本开发机可选样例；其他机器可不设置该变量并看到对应跳过项。测试包括真实 LangChain 对本机假 HTTP 服务的协议检查、脚本模型 + 真实 Docker 的错误候选重试，以及持久化/工具/验证器回归。它们不替代真实 DeepSeek 请求，真实运行单独记录。
