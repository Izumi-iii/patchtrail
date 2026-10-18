"""Workbench behavior: real Git inspection, explicitly no model repair yet."""

import hashlib
import json
import os
import re
import subprocess
import threading
from pathlib import Path, PurePosixPath


class RequestError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def text_field(data, key, maximum, *, required=False):
    value = data.get(key, "")
    if not isinstance(value, str):
        raise RequestError(f"{key} 必须是文本。")
    value = value.strip()
    if "\x00" in value or len(value) > maximum:
        raise RequestError(f"{key} 超出长度限制或包含无效字符。")
    if required and not value:
        raise RequestError(f"{key} 不能为空。")
    return value


def inspect_repository(raw_path):
    """Read Git metadata only. Never import code or run project commands."""
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        return {"status": "invalid", "error": "请填写本机 Git 仓库的绝对路径。"}
    try:
        path = path.resolve(strict=True)
        if not path.is_dir():
            return {"status": "invalid", "error": "该路径不是文件夹。"}
        env = os.environ.copy()
        # Do not inherit an unrelated Git work tree/index from the launcher.
        for name in list(env):
            if name.startswith("GIT_"):
                env.pop(name)
        env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")

        def git(*args):
            result = subprocess.run(
                ["git", "--no-optional-locks", "-c", "core.fsmonitor=false",
                 "-c", "core.untrackedCache=false", "-C", str(path), *args],
                capture_output=True, timeout=12, env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode:
                raise RequestError("无法读取 Git 信息，请确认路径是已提交过代码的 Git 仓库。")
            if len(result.stdout) > 2_000_000:
                raise RequestError("仓库信息超过当前预览版的大小限制。")
            return result.stdout.decode("utf-8", errors="replace").strip()

        root = git("rev-parse", "--show-toplevel")
        if Path(root).resolve() != path:
            return {"status": "invalid", "error": f"请选择仓库根目录：{root}"}
        head = git("rev-parse", "--verify", "HEAD")
        branch = git("branch", "--show-current") or "detached HEAD"
        dirty = bool(git("status", "--porcelain", "--untracked-files=normal"))
        files = [item for item in git("ls-files", "-z").split("\x00") if item]
        python_files = [name for name in files if name.endswith(".py")]
        tests = [name for name in python_files if PurePosixPath(name).name.startswith("test_")
                 or PurePosixPath(name).name.endswith("_test.py")]
        return {"status": "dirty" if dirty else "ready", "path": str(path),
                "name": path.name, "commit": head, "branch": branch, "dirty": dirty,
                "tracked_files": len(files), "python_files": len(python_files),
                "test_files": len(tests), "test_samples": tests[:8]}
    except FileNotFoundError:
        return {"status": "invalid", "error": "目录不存在，或 Git 命令不可用。"}
    except (OSError, subprocess.TimeoutExpired, RequestError) as exc:
        message = str(exc) if isinstance(exc, RequestError) else "读取仓库失败或超时，请检查目录与 Git。"
        return {"status": "invalid", "error": message}


def prepare_answer(content, repository_path, mode):
    base = {"kind": "workbench_preview", "agent_connected": False,
            "tests_executed": False, "mode": mode}
    if mode == "note":
        return ("需求已保存到这个会话。\n\n"
                "这条消息只记录需求，没有调用模型或执行测试。"
                "补充复现步骤与预期行为后，可切换「修复任务」并确认修复设置。", base)
    if not repository_path:
        return ("任务已保存。请先通过输入框旁的「关联仓库」填写本机 Git 仓库的绝对路径，"
                "再发送任务以检查仓库。\n\n"
                "本次为只读仓库检查；要生成补丁，请切换「修复任务」并配置任务来源。",
                {**base, "status": "needs_repository"})
    repository = inspect_repository(repository_path)
    metadata = {**base, "repository": repository, "status": repository["status"]}
    if repository["status"] == "invalid":
        return (f"任务已保存，仓库检查未通过。\n\n{repository['error']}\n\n"
                "修改仓库路径后可以重新检查；没有运行项目代码或测试。", metadata)
    details = ["仓库检查已完成（只读）。", "",
               f"仓库：{repository['name']}", f"分支：{repository['branch']}",
               f"基准提交：{repository['commit']}",
               f"已跟踪文件：{repository['tracked_files']} 个",
               f"Python 文件：{repository['python_files']} 个",
               f"按文件名发现的测试文件：{repository['test_files']} 个"]
    if repository["dirty"]:
        details += ["", "工作区存在未提交或未跟踪的改动。正式修复前需要先整理这些改动；"
                    "本次检查没有修改或提交任何文件。"]
    else:
        details += ["", "Git 工作区干净；这只通过了工作区检查，尚未验证依赖、测试或沙盒。"]
    details += ["", "本次没有调用模型、生成补丁或执行测试。要执行修复，请切换「修复任务」并确认任务与测试设置。"]
    return "\n".join(details), metadata


class Workbench:
    def __init__(self, store, jobs=None):
        self.store = store
        self.jobs = jobs
        self._lock = threading.Lock()
        self._busy = set()
        self._job_locks = {}

    def _enter(self, conversation_id):
        with self._lock:
            if conversation_id in self._busy:
                raise RequestError("这个会话正在处理请求，请稍后再试。", 409)
            self._busy.add(conversation_id)
        if self.jobs:
            from .jobs import execution_lock
            context = execution_lock(self.jobs.root)
            try:
                context.__enter__()
                self._job_locks[conversation_id] = context
                self.jobs.ensure_idle(conversation_id)
            except Exception:
                self._leave(conversation_id)
                raise

    def _leave(self, conversation_id):
        context = self._job_locks.pop(conversation_id, None)
        if context:
            context.__exit__(None, None, None)
        with self._lock:
            self._busy.discard(conversation_id)

    def update(self, conversation_id, data):
        if not data or set(data) - {"title", "repository_path"}:
            raise RequestError("只支持修改会话名称和仓库路径。")
        changes = {}
        for name, maximum in (("title", 100), ("repository_path", 2048)):
            if name in data:
                changes[name] = text_field(data, name, maximum, required=name == "title")
        self._enter(conversation_id)
        try:
            return self.store.update(conversation_id, changes)
        finally:
            self._leave(conversation_id)

    def delete(self, conversation_id):
        self._enter(conversation_id)
        try:
            self.store.delete(conversation_id)
        finally:
            self._leave(conversation_id)

    def send(self, conversation_id, data):
        if data.get("mode") == "repair" and self.jobs:
            return self.jobs.submit(conversation_id, data)
        content = text_field(data, "content", 12000, required=True)
        request_id = text_field(data, "request_id", 80, required=True)
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", request_id):
            raise RequestError("request_id 格式无效。")
        mode = data.get("mode", "inspect")
        if mode not in ("inspect", "note"):
            raise RequestError("该运行模式尚未支持。")
        self._enter(conversation_id)
        try:
            conversation = self.store.get(conversation_id)
            repository_path = conversation["repository_path"]
            fingerprint = hashlib.sha256(json.dumps(
                [content, mode], ensure_ascii=False).encode()).hexdigest()
            existing = self.store.submission_hash(conversation_id, request_id)
            if existing is not None:
                if existing != fingerprint:
                    raise RequestError("重复请求的内容不一致，请重新发送。", 409)
                return conversation
            answer, metadata = prepare_answer(content, repository_path, mode)
            return self.store.append_exchange(conversation_id, request_id, fingerprint,
                                              content, answer, metadata, repository_path)
        finally:
            self._leave(conversation_id)
