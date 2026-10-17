# DeepSeek 真实模型小样例验证

日期：2026-10-16。模型：deepseek-flash。端点：DeepSeek 官方 OpenAI-compatible 接口。思考模式关闭。

## 任务与结论

人工准备的小型退款仓库包含一个 Bug：refund_amount 忽略优惠金额。要求改为原价减优惠，同时保留无优惠订单行为。

结果：真实模型生成的候选补丁通过了独立容器中的回归、反馈与最终保留验收。没有修改输入仓库，没有使用脚本模型替代真实请求。

这是单个小样例，不是通用成功率评估。本次包含开发者修正工具格式错误分类后重跑，不能称为一次零干预成功；真实模型面对测试失败的自动重试效果仍需后续固定任务集评估。

## 两次执行均保留

| 执行 | 模型请求 | 进入验证的候选 | 结果 |
| --- | --- | --- | --- |
| agent-265c97b633144e1f8e12565b76262ba8 | 2 | 0 | POLICY_REJECTED / inconsistent_patch_header |
| agent-99c8dea25f2e4df195327f3c24f3b15b | 3 | 1 | PASSED / all_required_checks_passed |

首次模型给出 `return original - discount`，但省略 diff --git 头。原解析器将格式缺失判为策略拒绝。随后将这种安全范围内的缺失改为可修正反馈 missing_git_header，并在系统提示中明确完整 Git 补丁头要求；路径越界和测试修改仍是硬拒绝。首次记录保持原样，没有回写历史状态。

第二次只允许剩余的 6 次调用，实际用了 3 次。两次合计 5 次模型调用、1 个验证候选，低于用户授权的总计 8 次调用和 2 个候选。

## 独立验收证据

成功运行的最终验证 ID：verify-13e9a136e74c488aa52b4583d8c39c2f。

- 基准回归通过，基准反馈和保留测试能复现目标失败。
- candidate_regression：通过。
- candidate_feedback：通过。
- candidate_holdout：通过。
- 最终补丁哈希与 Agent 选定补丁一致，关键 manifest 内容哈希核对通过。
- 保留验收未返回模型循环，完整结果仅保存在本机私有证据中。

## 使用量与费用

提供商返回的两次执行使用量合计：

| 项目 | 数值 |
| --- | --- |
| 输入 token | 7,583（包含提供商计入的缓存输入） |
| 输出 token | 610 |
| 总 token | 8,193 |
| 费用 | 未核算，cost_usd=null、cost_status=unknown |

没有用零填充未知费用，也没有按未核对的单价估算。实际账单以提供商后台为准。

## 本机产物

同一实现的最终完整回归：49 项 unittest 全部通过，包含真实 Docker 验证、脚本模型重试、LangChain 本机 HTTP 协议与 Web 服务回归。测试日志位于 `.test-artifacts/agent/test-output-final.log`；其中脚本/本机协议测试不计入上述真实模型使用量。

`.test-artifacts/agent/deepseek-smoke-summary.json` 汇总两次执行；原始结果分别位于 deepseek-smoke.json 与 deepseek-smoke-retry.json。

`.patchtrail/agent-runs/<上述 run-id>/` 保存图、模型/工具私有消息、候选和验证引用。成功验证的完整证据位于其 verification-runs 子目录。以上运行目录被 Git 忽略，不随该说明公开上传。

密钥只保存在本机忽略文件 `.env`。发现 `.env.example` 被填入密钥后，已将示例密钥项清空并保留 `.env` 不变。本文不包含密钥或原始授权头。
