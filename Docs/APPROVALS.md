# 人工审批与补丁应用

日期：2026-10-18。本阶段交付本地 CLI 审批及应用操作的持久化，不安装依赖、不调用模型、不接入网页。后续同日已实现 [模型执行检查点恢复](CHECKPOINT_RECOVERY.md)，两种恢复分别管理模型运行与已批准的文件应用。

## 边界与状态

已通过最终独立验收的 Agent 运行可生成审批。冻结基准和候选源码、补丁、验证结果及清单；绑定目标仓库、基准提交、候选树、补丁、环境、验证计划、策略版本与有效期。审批状态保存在 SQLite，原 Agent result.json / manifest 不回写。

```text
WAITING_APPROVAL → APPROVED → APPLYING → APPLIED
                → REJECTED
                → EXPIRED / INVALIDATED
APPLYING → RECOVERY_REQUIRED → 显式 recover 对账
```

批准与应用是两个命令。批准必须提供查看时的 binding_sha256，不接受模型调用 approve。当前使用本机 OS 用户作审计标签，不提供远程身份认证或防本机管理员篡改的签名。

第一版只应用已有的允许修改的 Python 文件，拒绝创建、删除、重命名、模式变更、测试/配置文件修改，以及超出 TaskSpec 的变更。审批不扩大策略能力。输入仓库须为普通 Git 仓库，HEAD 精确等于基准、工作区和索引干净；要修改的文件字节须与冻结基准一致。变更文件需要 LF 格式。不会提交 Git 或推送。

## 恢复与并发

同一目标仓库使用 .git 内的 OS 文件锁，协调所有 PatchTrail 审批实例；进程退出后锁自动释放。锁文件可保留，不是遗留运行标志。其他编辑器/Git 并不遵守该锁，因此还需检查 HEAD、索引、路径、内容哈希和工作区变化；这是受控单用户仓库方案，不是文件系统级事务。

应用前先持久化 operation_id 和逐文件前后哈希，再以同目录临时文件及 os.replace 写入。单文件替换是原子的，多文件应用不是物理原子事务。进程崩溃后，显式 recover 检查文件：全部为目标内容则补记 APPLIED；部分为原文、部分为目标内容时仅补齐原文文件；出现未知内容、索引/HEAD 改变或过期后仍需写入时，进入 RECOVERY_REQUIRED，不覆盖或自动回滚他人内容。

重复批准不会产生新决定；重复应用已完成操作不再写文件。冲突决定、过期审批、哈希不符和非批准状态均拒绝应用。等待审批及已批准但未应用的状态可在进程重启后继续。冻结产物独立于原模型运行目录，原运行归档后仍可核对和应用。

## 实施验收

覆盖：批准、拒绝、过期、候选/产物篡改、HEAD/工作区/索引变化、重复/冲突决定、重复应用、跨进程恢复、应用中断与外部编辑保护，以及受保护路径。用临时样例测试实际文件应用，不修改正式项目工作区；不为了审批测试发起付费模型请求。

## 命令行操作

复用已有 PASSED 的 Agent 运行不调用模型，也不需要重新启动 Docker：

```powershell
.\.venv\Scripts\python.exe approval.py request --run '已有运行的绝对路径'
.\.venv\Scripts\python.exe approval.py list
```

从返回值中复制 `id` 和完整 `binding_sha256`，查看目标仓库、基准、验证身份、修改文件和原始 diff 后再作决定。示例中的变量由操作者填写，程序不会自动同意：

```powershell
$approvalId = 'approval-替换为返回的ID'
$reviewedBinding = '替换为审核时的完整binding_sha256'
.\.venv\Scripts\python.exe approval.py show --id $approvalId --diff
.\.venv\Scripts\python.exe approval.py approve --id $approvalId --binding $reviewedBinding
.\.venv\Scripts\python.exe approval.py apply --id $approvalId --binding $reviewedBinding
```

拒绝使用 `reject`，中断后使用 `recover`，参数与 `apply` 相同。`recover` 仍要求已经批准；不能绕过人工决定。遇到未知内容时先人工检查并恢复到记录的前/后内容，再重试；工具不自动回滚、重置或清理仓库。过期而尚有文件未写入的操作不能继续，需人工处理现场并重新验证。

默认保存于项目 `.patchtrail/approvals/`：`approvals.sqlite3` 包含状态与事件；每个审批目录包含冻结的完整 base/candidate 源码树、changes.patch、verification.json、task.json、bundle-manifest.json。该目录是私有本地数据，不公开上传。自定义存储：`approval.py --store PATH <命令>`。默认有效期 24 小时，创建时可指定 `--ttl-hours 1..168`。

新模型运行可以加 `--request-approval`，通过最终验证后 CLI 返回 `status=WAITING_APPROVAL`、`verification_status=PASSED`、原始 `agent_result` 和 `approval`。CLI 成功退出只表示待审批记录已建立，不表示已批准/应用。不加此参数保留原有行为。该模型入口会产生原有模型调用费用；单独 `approval.py` 不调用模型。

审批绑定是内容一致性检查，不是数字签名；有权同时改写数据库和产物的本机用户仍能伪造记录。审批人字段是 OS 用户名审计标签，不是登录认证。冻结 bundle 保留验证摘要和身份，完整 pytest 日志仍在原运行目录；若需归档完整验收证据，应同时保存原运行。

## 验证方式

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_approvals.py -v
```

审批专用测试用真实临时 Git 仓库及实际文件替换；验证阶段报告由测试 FakeBackend 提供，不能计入真实 Docker 或模型成功率。集成测试另使用实际 LangGraph + 脚本模型生成产物，并检查 `--request-approval` 不改写原验收证据。子进程测试用 `os._exit(73)` 在首个文件写入后硬退出，再从另一个进程执行恢复，并检查 OS 仓库锁跨进程互斥。

2026-10-18 本机验证：开启 Docker 测试及本机缺 pytest 对照镜像后，完整回归 **103 项全部通过，无跳过**，用时 351.319 秒；其中新增审批用例 25 项。日志位于本机 `.test-artifacts/approval-full-regression.log`（不入 Git）。容器检查包括正确/错误补丁、缺 pytest、空/跳过测试、缺报告、超时清理及脚本模型候选重试。

另复用上一轮真实 DeepSeek 的 `checkout-discount` 两文件产物成功创建并查看待审批记录；状态保持 WAITING_APPROVAL，目标样例仓库仍为基准内容，未批准或应用。本轮没有新增付费模型请求。此检查只证明已有真实运行产物能进入审批，不构成新的模型评估。
