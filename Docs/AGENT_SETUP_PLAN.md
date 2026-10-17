# Agent 依赖与模型接入计划

本文件是可审阅的安装计划，不表示依赖已经安装或模型已经配置。

执行记录（2026-10-16）：用户已授权创建项目环境并安装。现已使用本机 Python 3.12.2 创建 `.venv`，导入检查和 pip check 通过；44 个包版本已记录到 requirements-agent.lock.txt。DeepSeek 的具体配置、运行边界和测试方式见 [AGENT_RUNTIME.md](AGENT_RUNTIME.md)。

## 安装范围

- 在 PatchTrail 仓库内创建 `.venv`，不改系统 Python、不复用其他项目虚拟环境。
- 首选本机已有 Python 3.12；不可用时核对 3.14 与依赖的兼容性再决定。
- 安装项目可选依赖：LangChain、LangChain OpenAI-compatible 适配器、LangGraph；约束见 pyproject.toml 的 agent extra。
- 使用该环境解析出的版本生成 agent 依赖锁记录，验证导入和无网络的工具/图测试。
- 保持 Web 与独立 verify.py 的标准库启动方式可用，不强迫它们依赖模型框架。
- 安装不发起模型请求，不产生模型费用；真实模型 smoke 测试需要本项目明确配置。

## 配置与凭证

使用项目专用环境变量：PATCHTRAIL_MODEL、PATCHTRAIL_API_KEY、可选 PATCHTRAIL_BASE_URL。不自动读取其他项目的 .env 或凭证，不让用户在聊天里发送密钥。

第一版接入支持标准工具调用的 OpenAI-compatible chat endpoint。不同提供商不保证完全兼容；实际工具调用需要单独测试。缺少配置时明确报 CONFIG_ERROR，不退回预置补丁或规则模型。

## 安装后验证

1. 核对虚拟环境解释器与依赖版本。
2. 运行本地检索、工具权限、预算、LangGraph 路由与重试测试。
3. 使用脚本模型 + 真实 Docker 验证闭环，但标明它不是真实模型修复效果。
4. 只有明确配置模型后，才运行小预算真实请求，并报告调用量、未知费用和实际结果。

## 回退

本次不修改已有全局依赖或系统设置。若安装中断，保留日志并重新检查 `.venv`；不删除其他目录。原 Web 与 verify.py 仍可使用原解释器运行。需要清理项目环境时，先确认解析后的路径确实为本仓库 `.venv`。
