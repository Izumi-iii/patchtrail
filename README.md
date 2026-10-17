# PatchTrail

面向独立验证、有限重试与人工审批的代码修复 Agent 项目。目标架构采用 LangChain / LangGraph。

当前已有 **Web 工作台、独立补丁验证器与 LangChain / LangGraph 后端闭环**。网页仍用于会话与只读仓库检查；`verify.py` 验证人工补丁，`run_agent.py` 通过 DeepSeek 工具调用生成候选并独立验证。网页尚未连接 Agent 执行，页面回复仍不是模型回答。真实模型效果以运行证据为准。

## 本地启动

需要 Python 3.12+ 和 Git。网页与手工补丁验证器的控制端只用 Python 标准库；Agent 使用独立 `.venv`。网页已在 Windows + Python 3.14 + Chromium 中验证。

在仓库根目录运行：

```powershell
python app.py
```

打开 [本地工作台](http://127.0.0.1:8766)。按 `Ctrl+C` 停止服务。

如果端口被占用：

```powershell
python app.py --port 8770
```

此时打开 [8770 端口的工作台](http://127.0.0.1:8770)。默认会话数据保存在仓库根目录的 `.patchtrail/workbench.sqlite3`，已加入 Git 忽略；从同一目录重新启动即可恢复。自定义数据目录使用 `--data-dir PATH`。

这是仅绑定 `127.0.0.1` 的本地开发服务，不用于公网部署。不同服务实例应使用不同端口和数据目录。

## 可以试什么

1. 新建会话，输入一个 Bug 的复现步骤和预期行为，选择「记录需求」并发送。
2. 点击「关联仓库」，填写本机 Git 仓库**根目录的绝对路径**，例如 `D:\aaaWang\patchtrail`。
3. 选择「仓库检查」，发送检查请求，查看真实的提交、分支、工作区状态和按文件名发现的 Python 测试线索。
4. 新建或切换会话，尝试搜索、重命名、导出 JSON 和删除确认；刷新页面确认历史保留。

`Enter` 发送，`Shift+Enter` 换行。请求失败会保留输入。仓库存在未提交改动时只会提示，不执行重置、提交或清理。

网页中的仓库检查不会运行代码或测试，发现测试文件也不表示测试可运行。网页尚未接入模型执行、审批、取消修复任务或流式模型输出；真实 Agent 通过下文的独立入口运行。

## 测试

不需要模型凭证，也不会产生模型费用：

```powershell
python -m unittest discover -s tests -v
node --check src/patchtrail/web/static/app.js
```

真实浏览器交互测试需要 Node.js 和已有的 Playwright/Chromium 环境。若 Playwright 不在默认模块搜索路径，可通过环境变量指定安装位置：

```powershell
$env:PATCHTRAIL_PLAYWRIGHT_MODULE = '你的 Playwright 模块目录'
node tests/webui/workbench.browser.cjs
```

测试会启动独立临时服务、创建临时 Git 样例仓库，并在结束后回收；不会使用你的正式会话数据。截图和检查结果写入 `.test-artifacts/webui/`。没有可用浏览器时不能把该项标记为通过。

## 独立验证人工补丁

需要 Docker 与本地已有的 `patchtrail-pytest:0.1` 镜像。镜像配方和准备方式见 [环境说明](environments/pytest/README.md)，验证器不会自动安装或拉取依赖。

```powershell
python examples/create_demo.py --output .patchtrail/demo
python verify.py --task .patchtrail/demo/task.json --patch .patchtrail/demo/correct.patch
```

样例目录存在时拒绝覆盖；可以直接复用其中的 task.json，或指定新的输出目录。正确补丁应返回 PASSED，wrong.patch 与 regression.patch 应失败。状态与证据写入 `.patchtrail/runs/`，不会修改输入仓库。

真实容器测试需要显式开启：

```powershell
$env:PATCHTRAIL_DOCKER_TESTS = '1'
python -m unittest discover -s tests -v
```

未开启时 Docker 用例标记为 skipped。详细契约、支持范围、产物和限制见 [验证器说明](Docs/VERIFIER.md)。

## 文档与下一步

Agent 后端需要项目 `.venv`、准备好的 Docker 镜像和本机 `.env`（参考空密钥模板 `.env.example`）。小预算运行：

```powershell
.\.venv\Scripts\python.exe run_agent.py --task .patchtrail/verification-demo/task.json --max-model-calls 8 --max-candidates 2
```

此命令会调用配置的模型并可能产生费用；缺少密钥时明确拒绝，不使用预置补丁兜底。默认 DeepSeek 地址为 https://api.deepseek.com，模型 ID 可配置。完整配置、依赖锁及当前限制见下方运行说明。

- [完整设计](Docs/PROJECT_DESIGN.md)：目标架构、信任边界、补丁验收与里程碑。
- [Web 工作台实现说明](Docs/WEB_WORKBENCH.md)：本阶段边界、接口、数据与验证方式。
- [独立验证器](Docs/VERIFIER.md)：固定 commit、补丁重放、容器 pytest 与真实证据。
- [Agent 运行说明](Docs/AGENT_RUNTIME.md)：DeepSeek 配置、LangGraph 工具循环、有限重试及保留集边界。

后续优先补充真实模型评估、人工审批和持久化恢复，再连接现有网页；UI 优化继续延后。
