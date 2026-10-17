# 独立补丁验证器：M1 实施说明

日期：2026-10-16。

本阶段实现一个不依赖模型的后端垂直切片：明确任务与 Git commit → 干净源码快照 → 基准测试 → 应用人工补丁 → 独立容器测试 → 确定性结论与证据归档。UI 暂不改动，现有页面仍只有会话和仓库检查。

## 1. 已完成与保留边界

已实现：

- 标准库 dataclass + 显式字段校验的 TaskSpec，拒绝未知字段、短 SHA、分支名、非法路径和越界参数。
- 从完整 commit 的 Git blob 导出源码，不复制用户工作区、不执行 Git hooks 或 checkout filters。
- 拒绝脏工作区、符号链接、子模块、LFS 占位内容、不支持的文件路径及超大快照。
- 白名单 Python 源码补丁检查、干净重放、原始/规范化补丁哈希及候选内容清单。
- 原始回归必须通过，反馈测试必须在基准版本复现断言失败；无法复现则拒绝任务。
- 每个套件启动独立 Linux 容器，使用固定镜像 ID、非 root、禁网、只读根文件系统、只读源码/测试/控制挂载及资源上限。
- 检查容器实际权限和挂载，验证关键模块来自 `/workspace`，避免测到镜像中预装的旧包。
- 校验测试身份、setup/call/teardown 证据、退出码、skip/xfail、收集错误、超时、OOM 和报告身份。
- 结果、阶段日志、环境、实际容器策略、源码清单、事件、补丁及产物哈希落盘。
- 正常结束、失败、超时和可捕获异常路径回收本次创建的容器。

模型生成补丁、LangChain 工具循环与 LangGraph 有限重试由独立 Agent 层实现，见 [AGENT_RUNTIME.md](AGENT_RUNTIME.md)，验证器本身不执行模型决策。仍未实现持久化检查点、人工审批、硬退出对账、Web 验证任务接口和任意项目环境自动准备。

这是受控、非对抗仓库的验证器。源码与 pytest 在同一个 Python 进程内执行，恶意代码可能干扰框架或伪造证据；随机 nonce、报告一致性检查和只读测试不能解决同进程对抗问题。通过只表示满足登记的检查范围。

## 2. 技术取舍

控制端仍无第三方依赖。TaskSpec 使用 dataclass 和严格解析，后续引入 Pydantic 时应保持契约兼容；当前不能声称已使用 Pydantic。

pytest 安装在独立镜像内，不修改宿主 Python。镜像配方见 [环境说明](../environments/pytest/README.md)。构建是单独的环境准备步骤，验证器不会自动 pull 或安装依赖。

镜像标签在执行前解析为实际 ID，所有阶段固定使用该 ID。配方当前固定 pytest 直接版本，但尚未锁定所有传递依赖；实际镜像 ID 才是本次运行的环境身份。

源码、测试和控制脚本只读挂载，没有可写的宿主产物挂载。适配器生成受限 JSON，通过带 phase 与随机 nonce 的 Base64 帧返回；控制端同时检查容器退出状态和报告结构。报告缺失、重复、身份不匹配或内容矛盾均不能通过。该通道避免依赖容器停止后已经消失的 tmpfs 文件。

输出超限时终止执行；单份归档日志可能截断，报告记录截断状态。nonce 只防止意外混淆，不是对恶意源码的认证机制。

## 3. 运行演示

在项目根目录执行：

```powershell
python examples/create_demo.py --output .patchtrail/demo
python verify.py --task .patchtrail/demo/task.json --patch .patchtrail/demo/correct.patch
python verify.py --task .patchtrail/demo/task.json --patch .patchtrail/demo/wrong.patch
python verify.py --task .patchtrail/demo/task.json --patch .patchtrail/demo/regression.patch
```

生成器只创建新目录，存在时拒绝覆盖。默认镜像 `patchtrail-pytest:0.1` 不存在时返回环境错误，不降级为宿主执行。正确补丁应通过；错误和回归破坏补丁应失败。Bug 与补丁均为人工预置，不是模型修复能力证据。

本次开发环境已保留 `.patchtrail/verification-demo/task.json` 及对应补丁，可以直接用该目录替换示例中的 `.patchtrail/demo`。

CLI 输出 JSON，PASSED 返回退出码 0，其他结果返回 1；命令行参数错误由 argparse 返回其错误码。

## 4. TaskSpec JSON

```json
{
  "schema_version": 1,
  "task_id": "refund-discount-001",
  "request": "优惠券订单按实付金额退款，保持无优惠券订单行为。",
  "repository_path": "D:/projects/shop",
  "base_commit": "REPLACE_WITH_FULL_40_OR_64_HEX_COMMIT",
  "image": "patchtrail-pytest:0.1",
  "regression_dir": "D:/checks/shop/regression",
  "feedback_dir": "D:/checks/shop/feedback",
  "holdout_dir": "D:/checks/shop/holdout",
  "editable_paths": ["shop"],
  "source_roots": ["."],
  "import_modules": ["shop.refund"],
  "timeout_seconds": 60,
  "max_changed_files": 5,
  "max_changed_lines": 250
}
```

这是结构示例，commit 与路径必须替换；生成器会提供有效配置。

| 字段 | 含义 |
| --- | --- |
| request | 明确的预期行为与兼容性要求，后续模型也使用同一描述 |
| repository_path | 绝对 Git 根目录；无修改和未跟踪文件 |
| base_commit | 完整提交，可以不是当前 HEAD；测试必须适用于它 |
| image | 本地已有 Linux 镜像，不由目标仓库授权 |
| regression_dir | 可信回归套件目录，基准必须通过 |
| feedback_dir | 可信反馈套件目录，基准必须有实际断言失败 |
| holdout_dir | 可选的终结性套件；未配置时标记 feedback_only |
| editable_paths | 具体文件或目录前缀，不是 glob；仍受硬保护规则约束 |
| source_roots | 相对快照的导入路径，例如 `.`、`src` |
| import_modules | 至少一个需要验证导入来源的关键模块 |
| timeout_seconds | 每个测试容器的运行上限，不是整个任务的总预算 |
| max_changed_files / lines | 硬修改上限，无人工审批覆盖机制 |

套件由控制端复制并计算哈希，不能由未来的模型修改。完整 result 与 holdout 日志属于私有评估证据，不能原样返回修复循环。

## 5. 验证顺序

```text
校验并归档补丁 → 固定镜像 ID → 导出基准 → 冻结可信套件
  → baseline_regression：必须通过
  → baseline_feedback：必须复现断言失败
  → baseline_holdout（可选）：必须能有效执行
  → 从基准重建并应用完整补丁
  → candidate_regression：身份不变且全通过
  → candidate_feedback：身份不变且全通过
  → candidate_holdout（可选）：终结性验收
  → 再次核对源码/测试内容清单 → 结论与证据
```

每个阶段使用新容器。首版只处理单个人工候选，没有修复重试。保留集信息不得用于之后自动修改同次任务。

## 6. 补丁与文件限制

- UTF-8 Git unified diff，最大 256 KiB；暂不支持带空白或带引号的路径格式。
- 支持允许范围内 `.py` 文件修改、新增和删除；新增文件纳入候选清单。
- 禁止测试文件、conftest.py、setup.py、sitecustomize.py、usercustomize.py、受保护目录、权限变化、重命名、二进制和符号链接补丁。
- 检查绝对路径、上级越界、Windows 保留名、控制字符和大小写冲突。
- 快照上限：2,000 文件、单文件 1 MiB、总内容 20 MiB。
- 源码来自原始 Git blob，不套用工作区 CRLF 转换。
- 传输补丁 CRLF 规范化为 LF，保留 submitted.patch 与 changes.patch 及两个哈希。候选绑定实际应用的 changes.patch。
- 暂不支持被修改源码的 Git blob 本身保存为 CRLF，遇到时明确返回配置错误。

## 7. 产物与身份

默认 `.patchtrail/runs/<run-id>/` 包含 task.json、submitted.patch、changes.patch、environment.json、base-tree.json、candidate-tree.json、suites.json、events.jsonl、result.json、report.md、manifest.json。

`verification/<stage>/` 保存可信控制配置、实际 runtime-policy.json、container.json、有限 stdout/stderr、结构化 report.json、summary.json 或 error.json。只有执行过的阶段才存在对应证据，未执行不等于通过。

身份字段包括 base_commit、submitted_patch_sha256、patch_sha256、base_tree_sha256、candidate_tree_sha256、verification_plan_sha256 和 image_id。未来审批应绑定这些对象，不能仅绑定会话标题。

manifest 覆盖证据文件，排除自身及完整 base/candidate/suites 目录；源码和套件由各自内容清单绑定。失败运行保留已经产生的证据。

## 8. 结果状态

| 状态 | 示例 |
| --- | --- |
| PASSED | 基准有效、补丁可重放、所有必需检查通过、证据一致 |
| REPAIR_FAILED | 补丁冲突、候选导入/语法错误、断言失败、回归、测试身份变化 |
| CONFIG_ERROR | 脏仓库、契约错误、基准回归失败、无法复现、源码换行不支持 |
| POLICY_REJECTED | 改写测试、路径越界、权限变化、超出范围 |
| ENVIRONMENT_ERROR | 镜像/pytest 缺失、容器故障、超时、OOM、报告缺失或矛盾 |
| SYSTEM_ERROR | 控制端 I/O、内部一致性故障 |
| CANCELLED | 用户中断并完成可执行的回收 |

未收集、skip/xfail、不完整测试阶段、未知 pytest 退出码和仅打印 passed 都不能算成功。超时先按不可完成处理，不在证据不足时断定具体业务错误。

## 9. 测试

```powershell
# 离线与 Web 回归：Docker 用例会明确跳过。
python -m unittest discover -s tests -v

# 开启真实容器测试，不下载镜像。
$env:PATCHTRAIL_DOCKER_TESTS = '1'
$env:PATCHTRAIL_TEST_IMAGE = 'patchtrail-pytest:0.1'
python -m unittest discover -s tests -v
```

缺少 pytest 的真实用例还需设置 `PATCHTRAIL_NO_PYTEST_IMAGE`，指向本地已有且不含 pytest 的 Python 镜像；未设置时单独跳过。开发机已用 `algo-coach-runner:0.1` 验证该分支，项目不依赖它运行其他测试。

FakeBackend 仅验证状态流转，不作为容器和修复效果证据。真实 Docker 用例覆盖正确/错误/回归补丁、空测试、跳过、报告缺失、超时回收和依赖缺失。证据保存到 `.test-artifacts/verification/`。

## 10. 下一步

后续进展：已接入单模型工具循环与 LangGraph 有限重试，见 [AGENT_RUNTIME.md](AGENT_RUNTIME.md)。为防止最终保留集泄漏，服务层新增 baseline/feedback/full 三种 purpose，对应 READY/FEEDBACK_PASSED/PASSED；原 verify.py 仍使用 full。

代码检索与补丁工具 → 真实 LangChain 调用 → LangGraph 有限重试 → 审批/硬退出恢复 → 服务层接入网页。当前有正常和可捕获异常回收，不声称已有进程崩溃后的持久化恢复。

官方参考：[Git apply](https://git-scm.com/docs/git-apply)、[Docker run](https://docs.docker.com/reference/cli/docker/container/run/)、[pytest 退出码](https://docs.pytest.org/en/stable/reference/exit-codes.html)。参数支持以本仓库实现和集成测试为准。
