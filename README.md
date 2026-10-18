# PatchTrail

基于 **LangChain / LangGraph 的本地 Python 代码修复 Agent**。输入修复需求和受控仓库任务，Agent 读取代码、通过工具生成补丁，在 Docker 中运行测试；最终独立验收通过后，由人审查、批准，再单独应用到源仓库。

项目处于可运行的原型阶段，已有对话式 Web 工作台和 CLI。当前重点是修复过程可验证、预算有限、执行可恢复，支持范围是预先配置测试和修改边界的 Python 仓库。

本 README 按 2026-10-18 的实现整理；长期设计与阶段性记录见文末文档。

[快速开始](#快速开始) · [网页使用](#网页使用) · [修复自己的仓库](#修复自己的仓库) · [命令行使用](#命令行使用) · [测试与评估](#测试与评估) · [常见问题](#常见问题)

## 当前能做什么

| 能力 | 当前实现 |
| --- | --- |
| 对话式工作台 | 会话管理、需求记录、只读仓库检查、修复任务提交与进度展示 |
| 代码检索与编辑 | 目录、字面搜索、按行读取、Python AST 定义查询；带哈希校验的精确文本替换 |
| 补丁生成 | 模型提出修改，由工具生成相对固定 Git 基准的累计补丁，限制修改路径和规模 |
| 测试与纠错 | Docker 中执行回归和反馈测试，在预算内修正；保留测试只用于最终独立验收 |
| 人工审批 | 冻结补丁与证据，查看 diff 后批准或拒绝；批准与应用分别操作 |
| 中途恢复 | 本地 SQLite LangGraph 检查点、网页暂停/继续、跨进程恢复与原预算校验 |
| 固定任务集 | 7 个修复样例和 1 个无效基准对照，支持参考补丁校验和真实模型评估 |

```text
需求 + TaskSpec → 环境与基准检查 → 模型分析 → 代码工具 / 结构化编辑
                                  ↑                   ↓
                                  └── 预算内纠错 ← 回归与反馈测试
                                                      ↓
                                                最终独立验收
                                                      ↓
                                        查看 diff → 人工批准 → 单独应用
```

模型不能自行宣布修复成功。`PASSED` 表示通过独立验证，`APPROVED` 表示人工已批准，只有 `APPLIED` 才表示补丁已应用到目标文件；整个过程不会自动创建 Git 提交或推送。

## 快速开始

以下命令使用 **Windows PowerShell**，均在仓库根目录执行。

### 1. 准备运行环境

需要 Git、Python 3.12，以及能运行 Linux 容器的 Docker Desktop。锁定依赖已在 Windows / Python 3.12 环境使用，其他平台需要单独验证。Node.js 仅用于前端检查和浏览器测试，不是启动网页的必要条件。

首次使用创建项目虚拟环境并安装依赖；已有可用 `.venv` 时直接复用：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-agent.lock.txt
.\.venv\Scripts\python.exe -m pip check
```

使用显式 Python 路径，无需激活虚拟环境。依赖版本见 [requirements-agent.lock.txt](requirements-agent.lock.txt)；运行任务时不会自动安装包。

### 2. 配置模型

仅在 `.env` 不存在时，从模板复制，避免覆盖已有配置：

```powershell
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
```

在本机编辑项目根目录 `.env`：

```dotenv
DEEPSEEK_API_KEY=在本机填写真实密钥
PATCHTRAIL_MODEL=deepseek-flash
PATCHTRAIL_BASE_URL=https://api.deepseek.com
```

这是项目默认配置，模型 ID 可按服务端可用模型调整。使用其他支持工具调用的 OpenAI-compatible 服务时，配置 `PATCHTRAIL_API_KEY`、`PATCHTRAIL_MODEL` 和 `PATCHTRAIL_BASE_URL`，并单独验证兼容性。当前适配器使用 Chat Completions，并发送 DeepSeek 的关闭思考参数，不能保证所有兼容接口直接可用。非空环境变量优先于文件同名项，`PATCHTRAIL_API_KEY` 优先于 `DEEPSEEK_API_KEY`。

`.env` 已被 Git 忽略，密钥只在本机填写，不通过网页或聊天发送。修复任务会调用模型并可能产生费用；打开网页、记录需求和仓库检查不调用模型。

### 3. 准备测试镜像

启动 Docker Desktop，确认后端可用，再显式构建独立 pytest 镜像：

```powershell
docker info
docker build -t patchtrail-pytest:0.1 environments/pytest
docker run --rm --network none patchtrail-pytest:0.1 -m pytest --version
```

最后一条应显示 `pytest 8.4.2`。已有该镜像时可跳过构建。首次构建需要联网获取基础镜像和依赖；验证任务自身禁用容器网络，不会自动拉镜像或安装依赖。真实项目需要的第三方包也应事先准备到专用镜像中，详见 [测试环境说明](environments/pytest/README.md)。

### 4. 启动工作台

```powershell
.\.venv\Scripts\python.exe app.py --port 8770
```

打开 [http://127.0.0.1:8770](http://127.0.0.1:8770)。端口被占用时换一个空闲端口，并访问对应地址；不传 `--port` 时默认使用 `8766`。

按 `Ctrl+C` 关闭网页服务。**已启动的后台修复不会因此被主动终止**，需要暂停时先在任务卡片操作。同一数据目录只允许一个活跃修复任务。此服务面向本机使用，尚无公网认证或多用户部署能力。

## 网页使用

### 先跑一个退款 Bug 示例

1. 新建会话，保留「修复任务」模式，修复设置选择「退款 Bug 示例」。
2. 点击「试试退款修复」，或输入下面的需求并发送。
3. 在任务回复中查看代码工具、测试阶段、模型请求次数和候选数量。
4. 验收通过后点击「查看补丁」，核对修改路径和 diff，再批准或拒绝。
5. 批准后点击「应用已批准补丁」，才会修改这次示例的目标文件。

示例需求：

> 修复退款金额：应该退还原价减去优惠金额，保持无优惠时的行为。请先定位代码，再修改并验证。

示例中的错误代码位于 `shop/refund.py`：

```python
def refund_amount(original, discount=0):
    return original
```

预期修复为 `return original - discount`：`refund_amount(100, 20)` 应从 `100` 变为 `80`，`refund_amount(100)` 仍为 `100`。模型实际生成的补丁和是否通过，以任务结果为准。

每次网页示例都会创建独立 Git 仓库，不修改 PatchTrail 源码或其他正式项目。网页单个修复任务上限为 **8 次模型请求、2 个候选**，恢复沿用原额度。预算上限不是费用上限，实际费用取决于服务端计费。

### 模式、进度与恢复

| 模式 | 用途 | 调用模型 |
| --- | --- | --- |
| 修复任务 | 执行示例或指定 TaskSpec，生成并验证补丁 | 是 |
| 记录需求 | 保存需求文本 | 否 |
| 仓库检查 | 读取关联仓库的 Git 状态和测试线索 | 否 |

`Enter` 发送，`Shift+Enter` 换行。网页支持会话搜索、重命名和 JSON 导出；刷新后可继续查看已有任务，不会重复提交。当前约每 1.5 秒轮询阶段进度，不提供逐 token 输出。

「暂停执行」在步骤边界生效，正在进行的模型请求或 pytest 需要先返回；已完成的最终结果可能先于暂停生效。暂停后可以继续，进程异常退出后可以恢复。若提示 `model_outcome_unknown`，说明请求可能已发送但响应未保存：默认不重发，明确重试会额外占用原额度，也可能再次计费。

网页使用本次修复需求，不自动把完整会话历史传给模型，也不提供通用闲聊。详细操作和状态见 [网页 Agent 说明](Docs/WEB_AGENT.md)。

## 修复自己的仓库

真实任务需要一个 **TaskSpec JSON 文件**，明确源码、基准提交、可信测试与允许修改范围；仅填写仓库路径不足以开始修复。

准备条件：

- 本机普通 Git 仓库，工作区干净，`HEAD` 与 `base_commit` 完全一致。
- 已有 Python 源码和可导入模块，测试依赖已包含在指定镜像中。
- 预先提供可信测试：回归测试在修复前通过，反馈测试在修复前能复现目标断言失败；可选保留测试只参与最终验收。
- 测试目录独立于允许编辑的源码范围。Agent 不负责生成作为自身验收依据的可信测试。

下面是字段示例，**路径与提交哈希均需替换**；任务文件建议放在目标仓库外，避免造成未跟踪文件：

```json
{
  "schema_version": 1,
  "task_id": "refund-fix",
  "request": "退款应为原价减去优惠金额，保持无优惠时的行为。",
  "repository_path": "D:/projects/shop-demo",
  "base_commit": "0123456789abcdef0123456789abcdef01234567",
  "image": "patchtrail-pytest:0.1",
  "editable_paths": ["shop"],
  "source_roots": ["."],
  "import_modules": ["shop.refund"],
  "regression_dir": "D:/projects/shop-checks/regression",
  "feedback_dir": "D:/projects/shop-checks/feedback",
  "holdout_dir": "D:/projects/shop-checks/holdout",
  "timeout_seconds": 60,
  "max_changed_files": 5,
  "max_changed_lines": 250
}
```

用 `git -C D:\projects\shop-demo rev-parse HEAD` 获取完整提交哈希，不使用分支名或短 SHA。JSON 路径可使用 `/`，仓库和测试目录须为已存在的绝对路径；`editable_paths`、`source_roots` 使用仓库相对路径。

`source_roots` 指定容器内导入源码的根目录；例如采用 `src/shop/` 布局时可设为 `["src"]`，并将 `editable_paths` 改为 `["src/shop"]`。`import_modules` 是需要通过导入检查的 Python 模块名。没有保留测试时可以省略 `holdout_dir`，此时验收不包含独立保留集。

例如，反馈目录中的 pytest 用例可以是：

```python
from shop.refund import refund_amount


def test_refund_subtracts_discount():
    assert refund_amount(100, 20) == 80
```

完整可运行的源码、三组测试和 TaskSpec 可由下节的 `examples/create_demo.py` 生成，适合先参考其目录结构。进入网页「修复设置」，选择「本机 TaskSpec JSON 文件」，填写任务文件的绝对路径并保存。发送消息会替换本次 `request`，其余测试和范围约束保持不变。「关联仓库」仅用于只读检查，不等同于配置修复任务。

当前支持受限范围内**已有 Python 源文件的修改**，不支持自动新增、删除或重命名文件，不允许修改测试、依赖配置等受保护内容。完整约束见 [验证器契约](Docs/VERIFIER.md)。

## 命令行使用

### 生成样例并独立验证补丁

以下命令不调用模型，需要已有 Docker 测试环境：

```powershell
.\.venv\Scripts\python.exe examples/create_demo.py --output .patchtrail/demo
.\.venv\Scripts\python.exe verify.py --task .patchtrail/demo/task.json --patch .patchtrail/demo/correct.patch
```

生成器创建独立仓库、测试、`task.json` 和三份参考补丁。输出目录存在时拒绝覆盖，可以复用已有样例，或换一个新目录。`correct.patch` 应得到 `PASSED`，`wrong.patch` 和 `regression.patch` 应被拒绝。验证不会修改输入仓库，证据默认写入 `.patchtrail/runs/`。

### 运行真实模型

复用上一步生成的任务；此命令会产生模型请求：

```powershell
.\.venv\Scripts\python.exe run_agent.py --task .patchtrail/demo/task.json --max-model-calls 8 --max-candidates 2 --request-approval
```

运行产物默认在 `.patchtrail/agent-runs/`。最终验收通过后，`--request-approval` 创建 `WAITING_APPROVAL` 记录，**不会批准或应用**。不加该参数时，成功运行结束于 `PASSED`，之后仍可手工请求审批：

```powershell
.\.venv\Scripts\python.exe approval.py request --run '.patchtrail/agent-runs/agent-替换为运行ID'
```

### 审批与应用

先列出审批记录，再用真实 ID 查看冻结 diff；下面的 ID 和 binding 都是占位符，需替换为实际值：

```powershell
.\.venv\Scripts\python.exe approval.py list
.\.venv\Scripts\python.exe approval.py show --id 'approval-替换为实际ID' --diff
.\.venv\Scripts\python.exe approval.py approve --id 'approval-替换为实际ID' --binding '替换为已审查的binding_sha256'
.\.venv\Scripts\python.exe approval.py apply --id 'approval-替换为实际ID' --binding '替换为已审查的binding_sha256'
```

binding 来自 `show` 返回的记录，用于绑定所审查的补丁与证据。源仓库或证据变化时应用会拒绝。`reject` 用于拒绝，`recover` 用于恢复中断的应用，参数同样要求 ID 和 binding。默认审批有效期 24 小时；多文件应用可恢复，但不是文件系统级原子事务。详见 [审批说明](Docs/APPROVALS.md)。

### 恢复中断的 Agent

用实际运行目录替换占位符，恢复时不再传入预算参数：

```powershell
.\.venv\Scripts\python.exe run_agent.py --resume '.patchtrail/agent-runs/agent-替换为运行ID' --request-approval
```

新运行默认保存检查点；恢复校验原任务、文件、测试、实现和模型配置，不能任意移动运行目录或提高额度。旧版没有检查点的记录不能恢复。未知模型请求的显式重试方式及崩溃边界见 [检查点说明](Docs/CHECKPOINT_RECOVERY.md)。网页不自动导入已有 CLI 运行。

## 测试与评估

### 自动化测试

使用项目环境执行，不需要真实模型凭证，不产生模型费用：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --check src/patchtrail/web/static/app.js
```

第二条是可选的 JavaScript 语法检查，需要 Node.js。默认跳过显式门控的真实 Docker 用例；已有可用 Docker 和镜像时，在当前 PowerShell 会话开启后运行：

```powershell
$env:PATCHTRAIL_DOCKER_TESTS = '1'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
Remove-Item Env:PATCHTRAIL_DOCKER_TESTS
```

浏览器交互测试另外需要已有 Playwright 和 Chromium 环境：

```powershell
node tests/webui/workbench.browser.cjs
node tests/webui/agent.browser.cjs
```

可通过 `PATCHTRAIL_PLAYWRIGHT_MODULE` 指定 Playwright 模块目录，通过 `PATCHTRAIL_BROWSER_PATH` 指定浏览器可执行文件。测试使用临时会话和仓库，Agent 交互测试使用脚本模型，不代表真实 DeepSeek 效果；截图和日志写入 `.test-artifacts/`。

跳过项不算通过：例如缺 pytest 的镜像对照用例还需要 `PATCHTRAIL_NO_PYTEST_IMAGE` 指向已准备的无 pytest 镜像，完整条件见测试代码。

### 固定任务集

```powershell
.\.venv\Scripts\python.exe evaluate.py list
.\.venv\Scripts\python.exe evaluate.py validate --output .patchtrail/evaluations/validation-v1
```

`list` 只列出任务；`validate` 使用 Docker 验证 7 个修复样例的参考补丁及 1 个无效基准对照，不调用模型。输出目录必须是新目录，结果包含 `report.json`、`report.md` 和验证证据。参考补丁通过不计为模型成功。

真实模型评估使用 `evaluate.py run`，必须显式指定 `--allow-model-calls` 和 `--max-total-model-calls`，完整命令与判定方式见 [评估说明](Docs/EVALUATION.md)。

已有真实模型记录：2026-10-17 的 DeepSeek 新旧编辑协议各一轮对照，在同一批公开小样例上均为 **7/7 修复通过、1 个无效基准正确拒绝**。结构化编辑版使用 21 次模型请求、38,391 个已知 token；旧协议为 28 次请求、50,885 个已知 token。仅说明这批任务的单轮观察，不能外推通用仓库成功率、费用或稳定节省比例，见 [对照证据](Docs/evidence/deepseek-structured-comparison-2026-10-17.md)。

网页接入、浏览器交互和检查点恢复的测试记录另见 [网页验收记录](Docs/WEB_AGENT.md#本机验收记录)；脚本模型和本地假服务测试不计入真实模型评估。

## 目录与本地数据

```text
app.py / web_worker.py      网页服务与后台修复进程
run_agent.py / verify.py    Agent 与独立补丁验证入口
approval.py / evaluate.py   人工审批与固定任务集评估
src/patchtrail/
  agents/                  LangChain 适配、LangGraph 图和检查点
  tools/                   源码读取、检索、受控编辑
  verification/ sandbox/   独立验收与 Docker pytest
  approvals/ evaluation/   审批状态与评估执行
  web/                     HTTP 接口、会话、任务和前端静态文件
examples/                  可运行退款样例生成器
benchmarks/v1/             版本化固定任务集
environments/pytest/       测试镜像配方
tests/                     Python 与浏览器测试
Docs/                      设计、契约和评估证据
```

从仓库根目录按默认参数启动时，本地数据写入 `.patchtrail/`：

| 路径 | 内容 |
| --- | --- |
| `workbench.sqlite3` | 网页会话、消息与任务记录 |
| `web-jobs/` | 网页任务配置、独立样例和后台日志 |
| `r/` | 网页 Agent 运行与验证证据，使用短路径减轻 Windows 路径限制 |
| `agent-runs/` | CLI Agent 运行、检查点和结果 |
| `runs/` | 独立补丁验证结果 |
| `approvals/` | 审批记录与冻结产物 |
| `evaluations/` | 固定任务集评估报告 |

网页可用 `--data-dir PATH` 指定数据目录；同一数据目录重新启动可恢复历史。`.env`、`.venv`、`.patchtrail` 和 `.test-artifacts` 均被 Git 忽略。运行证据可能包含源码、消息和测试细节，应保留在本机；会话 JSON 导出不等于完整运行证据备份。

## 常见问题

| 现象 | 检查方式 |
| --- | --- |
| 页面提示连接被拒绝 | 确认 `app.py` 仍在运行，浏览器地址与实际端口一致；`8766` 和 `8770` 是不同服务地址 |
| 网页可打开，但修复失败 | 检查项目 `.venv`、根目录 `.env`、模型配置、Docker 后端和测试镜像；静态页面可访问不代表执行环境就绪 |
| Docker Desktop / WSL 无响应 | 先恢复 Docker，确认 `docker info` 和真实容器检查成功，再继续修复或评估 |
| 提示基准或仓库状态无效 | 确认工作区干净、完整提交哈希匹配、回归通过且反馈测试确实复现 Bug；不要靠跳过检查继续 |
| 提示样例或评估输出目录已存在 | 复用已有样例，或为新实验指定新目录；命令不会覆盖旧证据 |
| 暂停后还在等待 | 暂停是协作式的，当前模型请求或测试结束后才到可暂停边界 |
| 提示模型请求结果未知 | 先查看任务记录；只有愿意承担额外请求时才明确重试，原请求仍占额度 |
| 验收通过但源代码没变化 | 这是待审查候选，需查看补丁、批准，再单独应用；`PASSED` 不代表已应用 |

## 边界与后续方向

当前尚无远程 GitHub/GitLab 拉取、自动依赖解析、通用 shell、多语言修复、逐 token 输出或多租户支持。Docker 的限制用于当前可信控制端下的本地测试，不应将本项目视为运行任意恶意仓库的通用安全沙盒。恢复也不保证所有外部副作用只发生一次。

下一阶段优先增加真实仓库任务、失败分类与重复评估，检验当前小样例之外的适用范围；再扩展代码检索和网页体验。设计中的目标不等于已经实现的能力。

## 进一步阅读

- [完整设计](Docs/PROJECT_DESIGN.md)：目标架构、信任边界和里程碑。
- [网页 Agent](Docs/WEB_AGENT.md)：任务设置、进度、暂停恢复和审批应用。
- [Agent 运行](Docs/AGENT_RUNTIME.md)：模型配置、图执行、预算与工具边界。
- [结构化编辑](Docs/STRUCTURED_EDITING.md)：精确替换、哈希校验和累计补丁。
- [独立验证器](Docs/VERIFIER.md)：基准、可信测试、判定与产物。
- [人工审批](Docs/APPROVALS.md)：冻结证据、binding、应用及恢复。
- [检查点恢复](Docs/CHECKPOINT_RECOVERY.md)：持久化、跨进程恢复与未知请求。
- [评估入口](Docs/EVALUATION.md)：固定任务集、预算和真实模型结果解释。
