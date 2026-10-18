"""Persistent local jobs. HTTP never runs model inference in a request thread."""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from patchtrail.agents.config import ModelSettings
from patchtrail.approvals.service import ApprovalService
from patchtrail.domain.task import TaskSpec, VerificationError
from patchtrail.verification.service import write_json
from .service import RequestError, text_field
from .store import now

ROOT = Path(__file__).resolve().parents[3]
ACTIVE = ("QUEUED", "RUNNING", "STOP_REQUESTED")


@contextmanager
def execution_lock(directory):
    path = Path(directory) / "worker.lock"
    with path.open("a+b") as stream:
        if not path.stat().st_size:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RequestError("任务仍在执行，请等待当前步骤结束。", 409) from None
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class Jobs:
    def __init__(self, store, *, launcher=None, require_model=True):
        self.store = store
        self.root = (store.path.parent / "web-jobs").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.approvals = ApprovalService(store.path.parent / "approvals")
        self.launcher = launcher or self.launch
        self.require_model = require_model
        self.processes = {}
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, message_id TEXT NOT NULL,
                    task TEXT NOT NULL, state TEXT NOT NULL, attempt TEXT NOT NULL,
                    queued_at REAL NOT NULL, approval_id TEXT, error_code TEXT, error TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(conversation_id,request_id));
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_web_job ON jobs((1))
                    WHERE state IN ('QUEUED','RUNNING','STOP_REQUESTED');
            """)

    def capabilities(self):
        try:
            settings = ModelSettings.from_env(ROOT / ".env")
            return {"configured": True, "model": settings.model, "max_model_calls": 8, "max_candidates": 2}
        except VerificationError:
            return {"configured": False, "model": None, "max_model_calls": 8, "max_candidates": 2}

    def row(self, identity):
        if not re.fullmatch(r"job-[a-f0-9]{32}", identity):
            raise RequestError("任务 ID 无效。")
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise RequestError("任务不存在。", 404)
        return dict(row)

    def directory(self, identity):
        self.row(identity)
        path = self.root / identity
        if path.is_symlink() or path.resolve().parent != self.root:
            raise RequestError("任务目录不安全。", 409)
        return path

    def run_directory(self, identity):
        root = self.run_root(identity)
        paths = list(root.glob("agent-*"))
        if len(paths) > 1:
            raise RequestError("任务包含多个运行目录，需要人工检查。", 409)
        if paths and (paths[0].is_symlink() or paths[0].resolve().parent != root):
            raise RequestError("运行目录不安全。", 409)
        return paths[0] if paths else None

    def run_root(self, identity):
        self.row(identity)
        # Keep verifier/control paths below Windows MAX_PATH on normal workspaces.
        from patchtrail.approvals.service import checked_path
        return checked_path(self.root.parent, "r/" + identity[4:14])

    def reconcile(self, row):
        if row["state"] not in ACTIVE:
            return row
        # QUEUED covers process bootstrap and the write-ahead launch gap.
        if time.time() - row["queued_at"] < 10:
            return row
        try:
            with execution_lock(self.directory(row["id"])):
                with self.store.connect() as db:
                    db.execute("UPDATE jobs SET state='INTERRUPTED',updated_at=? WHERE id=? AND attempt=? AND state IN ('QUEUED','RUNNING','STOP_REQUESTED')",
                               (now(), row["id"], row["attempt"]))
        except RequestError:
            pass
        return self.row(row["id"])

    def ensure_idle(self, conversation_id):
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM jobs WHERE conversation_id=? AND state IN ('QUEUED','RUNNING','STOP_REQUESTED')",
                              (conversation_id,)).fetchall()
        if any(self.reconcile(dict(row))["state"] in ACTIVE for row in rows):
            raise RequestError("该会话有正在执行的修复任务，请先暂停。", 409)

    def reconcile_active(self):
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM jobs WHERE state IN ('QUEUED','RUNNING','STOP_REQUESTED')").fetchall()
        for row in rows:
            self.reconcile(dict(row))

    def configure(self, conversation_id, data):
        with execution_lock(self.root):
            return self._configure(conversation_id, data)

    def _configure(self, conversation_id, data):
        if set(data) - {"task_source", "task_path"}:
            raise RequestError("任务设置字段无效。")
        self.ensure_idle(conversation_id)
        source = data.get("task_source", "demo")
        path = text_field(data, "task_path", 2048)
        changes = {"task_source": source, "task_path": ""}
        if source == "file":
            if not Path(path).is_absolute():
                raise RequestError("任务文件需要绝对路径。")
            task = TaskSpec.load(path)
            changes.update(task_path=str(Path(path).resolve()), repository_path=task.repository_path)
        elif source != "demo":
            raise RequestError("未知的任务来源。")
        return self.store.update(conversation_id, changes)

    def submit(self, conversation_id, data):
        with execution_lock(self.root):
            self.reconcile_active()
            return self._submit(conversation_id, data)

    def _submit(self, conversation_id, data):
        if set(data) - {"content", "request_id", "mode"}:
            raise RequestError("修复请求字段无效。")
        content = text_field(data, "content", 12000, required=True)
        request_id = text_field(data, "request_id", 80, required=True)
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", request_id):
            raise RequestError("请求 ID 无效。")
        conversation = self.store.get(conversation_id)
        fingerprint = hashlib.sha256(json.dumps([content, conversation["task_source"], conversation["task_path"]], ensure_ascii=False).encode()).hexdigest()
        with self.store.connect() as db:
            existing = db.execute("SELECT * FROM jobs WHERE conversation_id=? AND request_id=?", (conversation_id, request_id)).fetchone()
        if existing:
            if existing["fingerprint"] != fingerprint:
                raise RequestError("重复请求内容或任务配置不一致。", 409)
            return self.store.get(conversation_id)
        if self.require_model and not self.capabilities()["configured"]:
            raise RequestError("请先在项目本机 .env 配置模型密钥；不要在聊天中输入密钥。", 409)
        self.ensure_idle(conversation_id)
        identity = "job-" + uuid.uuid4().hex
        directory = self.root / identity
        directory.mkdir()
        slot = self.root.parent / "r" / identity[4:14]
        slot.mkdir(parents=True, exist_ok=False)
        if conversation["task_source"] == "demo":
            from examples.create_demo import create_demo
            task = TaskSpec.from_dict(create_demo(directory / "fixture"))
        else:
            task = TaskSpec.load(conversation["task_path"])
        task = replace(task, request=content)
        write_json(directory / "task.json", task.to_dict())
        attempt, message_id, timestamp = uuid.uuid4().hex, uuid.uuid4().hex, now()
        try:
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM jobs WHERE state IN ('QUEUED','RUNNING','STOP_REQUESTED')").fetchone():
                    raise RequestError("本地已有修复任务在执行，请稍后再提交。", 409)
                db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (identity, conversation_id, request_id, fingerprint, message_id, json.dumps(task.to_dict()),
                            "QUEUED", attempt, time.time(), None, None, None, timestamp, timestamp))
                for mid, role, text, extra in ((uuid.uuid4().hex, "user", content, {}),
                        (message_id, "assistant", "修复任务已提交，正在准备运行环境与基准测试。", {"kind": "agent_job", "job_id": identity})):
                    db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?)", (mid, conversation_id, role, text, json.dumps(extra), now()))
                title = content.replace("\n", " ")[:32] if conversation["title"] == "新会话" else conversation["title"]
                db.execute("UPDATE conversations SET title=?,repository_path=?,updated_at=? WHERE id=?",
                           (title, task.repository_path, now(), conversation_id))
        except Exception:
            # Keep failed preparation artifacts for diagnosis; no model was called.
            raise
        self.start(identity, attempt, retry=False)
        return self.store.get(conversation_id)

    def start(self, identity, attempt, *, retry):
        try:
            self.launcher(identity, attempt, retry=retry)
        except Exception:
            self.finish(identity, attempt, "FAILED", error_code="worker_launch_failed", error="后台进程启动失败，请检查项目 Python 环境。")

    def launch(self, identity, attempt, *, retry):
        python = ROOT / ".venv/Scripts/python.exe" if os.name == "nt" else ROOT / ".venv/bin/python"
        if not python.is_file():
            raise OSError("Project environment missing")
        args = [str(python), str(ROOT / "web_worker.py"), "--database", str(self.store.path.resolve()),
                "--job", identity, "--attempt", attempt]
        if retry:
            args.append("--retry-unknown-model-call")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", LANGSMITH_TRACING="false")
        with (self.directory(identity) / "worker.log").open("ab") as output:
            process = subprocess.Popen(args, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                       env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.processes[identity] = process

    def finish(self, identity, attempt, state, *, approval_id=None, error_code=None, error=None):
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET state=?,approval_id=?,error_code=?,error=?,updated_at=? WHERE id=? AND attempt=?",
                       (state, approval_id, error_code, error, now(), identity, attempt))
            row = db.execute("SELECT * FROM jobs WHERE id=?", (identity,)).fetchone()
            if row and row["attempt"] == attempt:
                label = {"WAITING_APPROVAL": "独立验收已通过。请查看补丁，再批准或拒绝；尚未修改目标仓库。",
                         "SUSPENDED": "任务已暂停，可以继续执行。", "RECOVERY_REQUIRED": "任务需要恢复，请查看原因后操作。",
                         "FAILED": "修复任务结束，未通过验收。"}.get(state, "任务状态已更新。")
                db.execute("UPDATE messages SET content=? WHERE id=?", (label, row["message_id"]))
                db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now(), row["conversation_id"]))

    def public(self, identity):
        row = self.reconcile(self.row(identity))
        process = self.processes.get(identity)
        if process and process.poll() is not None:
            self.processes.pop(identity, None)
        result = {key: row[key] for key in ("id", "conversation_id", "state", "created_at", "updated_at", "error_code", "error")}
        task = json.loads(row["task"])
        result.update(repository_path=task["repository_path"], request=task["request"], base_commit=task["base_commit"],
                      budget={"max_model_calls": 8, "max_candidates": 2}, progress=None, timeline=[], verification=None, usage=None, approval=None)
        run = self.run_directory(identity)
        if run:
            result["run_id"] = run.name
            for filename, key in (("progress.json", "progress"), ("result.json", "run_result")):
                path = run / filename
                if path.exists():
                    value = json.loads(path.read_text(encoding="utf-8"))
                    if key == "progress":
                        result[key] = value
                    else:
                        result["usage"] = value.get("usage")
                        result["run_status"] = value.get("status")
                        result["reason_code"] = value.get("reason_code")
                        result["error_code"] = row["error_code"] or value.get("reason_code")
                        if value.get("final_verification_ref"):
                            final_path = Path(value["final_verification_ref"])
                            if final_path.resolve().is_relative_to(run / "verification-runs"):
                                final = json.loads((final_path / "result.json").read_text(encoding="utf-8"))
                                result["verification"] = {"status": final["status"], "validation_level": final["validation_level"],
                                                          "stages": final["stages"]}
            events = run / "events.jsonl"
            if events.exists():
                # Bound public output and tolerate a writer's incomplete final line.
                lines = events.read_text(encoding="utf-8").splitlines()[-25:]
                for line in lines:
                    try:
                        event = json.loads(line)
                        result["timeline"].append({k: event[k] for k in ("kind", "node", "name", "call", "status") if k in event})
                    except ValueError:
                        continue
        if row["approval_id"]:
            approval = self.approvals.get(row["approval_id"])
            result["state"] = approval["state"]
            result["approval"] = {k: approval[k] for k in ("id", "state", "binding_sha256", "expires_at", "changes", "decision")}
        labels = {
            "QUEUED": "修复任务已提交，正在准备运行环境与基准测试。",
            "RUNNING": "正在分析代码、生成候选并运行独立验证。",
            "STOP_REQUESTED": "已请求暂停，等待当前步骤结束。",
            "WAITING_APPROVAL": "独立验收已通过。请查看补丁，再批准或拒绝；尚未修改目标仓库。",
            "APPROVED": "补丁已批准，尚未应用。你可以单独执行应用操作。",
            "APPLIED": "补丁已按审批记录应用到目标文件。尚未创建 Git 提交。",
            "REJECTED": "补丁已拒绝，目标文件不会被应用此补丁。",
            "EXPIRED": "审批已过期，不能继续应用；需要重新验证并创建审批。",
            "INVALIDATED": "审批已失效，请核对仓库及产物变化。",
            "SUSPENDED": "任务已暂停，可以从检查点继续执行。",
            "INTERRUPTED": "执行进程已中断，任务记录仍保留，可以尝试恢复。",
            "RECOVERY_REQUIRED": "任务需要恢复。请检查原因；未知模型请求不会自动重发。",
            "FAILED": "任务结束，未通过独立验收；未应用补丁。",
            "APPLYING": "补丁应用尚未完成，可检查后恢复应用。",
        }
        label = labels.get(result["state"], "任务状态已更新。")
        with self.store.connect() as db:
            changed = db.execute("UPDATE messages SET content=? WHERE id=? AND content<>?", (label, row["message_id"], label))
            if changed.rowcount:
                db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now(), row["conversation_id"]))
        return result

    def list(self, conversation_id):
        self.store.get(conversation_id)
        with self.store.connect() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM jobs WHERE conversation_id=? ORDER BY created_at", (conversation_id,))]
        return [self.public(identity) for identity in ids]

    def diff(self, identity):
        row = self.row(identity)
        if not row["approval_id"]:
            raise RequestError("尚无通过验收的待审批补丁。", 409)
        approval = self.approvals.get(row["approval_id"], include_diff=True)
        return {"diff": approval["diff"], "binding_sha256": approval["binding_sha256"],
                "repository_path": approval["identity"]["repository_path"], "base_commit": approval["identity"]["base_commit"],
                "patch_sha256": approval["identity"]["patch_sha256"]}

    def action(self, identity, data):
        with execution_lock(self.root):
            self.reconcile_active()
            return self._action(identity, data)

    def _action(self, identity, data):
        if set(data) - {"action", "binding"}:
            raise RequestError("操作字段无效。")
        action = data.get("action")
        row = self.reconcile(self.row(identity))
        directory = self.directory(identity)
        if action == "pause":
            if row["state"] not in ACTIVE:
                raise RequestError("任务当前不在运行。", 409)
            (directory / "pause.requested").touch()
            with self.store.connect() as db:
                db.execute("UPDATE jobs SET state='STOP_REQUESTED' WHERE id=? AND state IN ('QUEUED','RUNNING')", (identity,))
        elif action in ("resume", "retry_unknown"):
            if row["state"] not in ("SUSPENDED", "INTERRUPTED", "RECOVERY_REQUIRED"):
                raise RequestError("该状态不能恢复。", 409)
            if self.require_model and not self.capabilities()["configured"]:
                raise RequestError("本机模型配置尚未就绪。", 409)
            with execution_lock(directory):
                with self.store.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    if db.execute("SELECT 1 FROM jobs WHERE state IN ('QUEUED','RUNNING','STOP_REQUESTED')").fetchone():
                        raise RequestError("已有修复任务在运行。", 409)
                    attempt = uuid.uuid4().hex
                    db.execute("UPDATE jobs SET state='QUEUED',attempt=?,queued_at=?,error=NULL,error_code=NULL WHERE id=? AND attempt=?",
                               (attempt, time.time(), identity, row["attempt"]))
                    (directory / "pause.requested").unlink(missing_ok=True)
                self.start(identity, attempt, retry=action == "retry_unknown")
        elif action in ("approve", "reject", "apply", "recover_apply"):
            if not row["approval_id"]:
                raise RequestError("任务尚未进入人工审批。", 409)
            binding = text_field(data, "binding", 64, required=True)
            if action in ("apply", "recover_apply"):
                with self.store.connect() as db:
                    if db.execute("SELECT 1 FROM jobs WHERE state IN ('QUEUED','RUNNING','STOP_REQUESTED')").fetchone():
                        raise RequestError("请先暂停正在执行的修复，再应用补丁。", 409)
                self.approvals.apply(row["approval_id"], binding, recover=action == "recover_apply")
            else:
                self.approvals.decide(row["approval_id"], binding, action)
        else:
            raise RequestError("未知操作。")
        return self.public(identity)
