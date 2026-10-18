"""Candidate-bound approvals. SQLite owns decisions; file hashes reconcile effects."""

import getpass
import json
import os
import re
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from patchtrail.domain.task import TaskSpec, VerificationError, safe_relative
from patchtrail.workspace.snapshot import digest, git, manifest, tree_hash, validate_patch

POLICY_VERSION = "approval-existing-python-v1"


def fail(reason, message):
    raise VerificationError("APPROVAL_ERROR", reason, message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def read_json(path):
    path = Path(path)
    if path.stat().st_size > 4_194_304:
        fail("artifact_size", "Approval JSON artifact exceeds 4 MiB.")
    return json.loads(path.read_text(encoding="utf-8"))


def checked_path(root, relative):
    safe_relative(relative)
    root = Path(root)
    path = root
    for part in Path(relative).parts:
        path = path / part
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                fail("unsafe_path", "Symlinks and reparse points are not supported.")
    if not path.resolve().is_relative_to(root.resolve()):
        fail("unsafe_path", "Path escapes the expected directory.")
    return path


def verify_files(root, files):
    for name, expected in files.items():
        path = checked_path(root, name)
        if not path.is_file() or digest(path.read_bytes()) != expected["sha256"]:
            fail("artifact_changed", "An approval or verification artifact changed.")


@contextmanager
def repository_lock(repository):
    repository = Path(repository)
    folder = repository / ".git"
    if not folder.is_dir() or folder.is_symlink() or getattr(folder.lstat(), "st_file_attributes", 0) & 0x400:
        fail("unsupported_repository", "Use a normal repository with a real .git directory.")
    path = folder / "patchtrail-approval.lock"
    if path.is_symlink() or path.exists() and (path.stat().st_nlink > 1 or getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
        fail("unsafe_path", "Repository lock must not be a symlink.")
    with path.open("a+b") as stream:
        if path.stat().st_size == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            yield
        except OSError as exc:
            if not acquired:
                fail("repository_busy", "Another PatchTrail operation holds the repository lock.")
            raise exc
        finally:
            if acquired:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class ApprovalService:
    def __init__(self, directory, *, clock=time.time):
        self.root = Path(directory).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.database = self.root / "approvals.sqlite3"
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY, run_path TEXT UNIQUE NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, approval_id TEXT NOT NULL,
                    timestamp REAL NOT NULL, kind TEXT NOT NULL, details TEXT NOT NULL);
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.database, timeout=10)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def _load(self, identity):
        if not re.fullmatch(r"approval-[a-f0-9]{32}", identity):
            fail("invalid_approval_id", "Invalid approval ID.")
        with self._db() as db:
            row = db.execute("SELECT data FROM approvals WHERE id=?", (identity,)).fetchone()
        if row is None:
            fail("approval_not_found", "Approval does not exist.")
        return json.loads(row[0])

    def _save(self, record, event, **details):
        with self._db() as db:
            db.execute("UPDATE approvals SET data=? WHERE id=?", (json.dumps(record), record["id"]))
            db.execute("INSERT INTO events(approval_id,timestamp,kind,details) VALUES(?,?,?,?)",
                       (record["id"], self.clock(), event, json.dumps(details)))

    @staticmethod
    def _binding(record):
        return digest(canonical({key: record[key] for key in
                                 ("id", "identity", "changes", "bundle_sha256", "created_at", "expires_at")}))

    @staticmethod
    def _temporary_paths(record):
        operation = record.get("operation", {})
        if not operation:
            return []
        identity = operation.get("id", "")
        if not re.fullmatch(r"apply-[a-f0-9]{32}", identity):
            fail("journal_changed", "Invalid application operation identity.")
        expected = [str(Path(name).parent / (".patchtrail-" + identity + f"-{i}.tmp")).replace("\\", "/")
                    for i, name in enumerate(sorted(record["changes"]))]
        if operation.get("temporary_paths") != expected:
            fail("journal_changed", "Application temporary file journal changed.")
        return expected

    def _bundle(self, record):
        if self._binding(record) != record["binding_sha256"]:
            fail("binding_changed", "Stored approval identity changed.")
        if record["identity"]["policy_version"] != POLICY_VERSION:
            fail("policy_changed", "Approval policy version changed; create a new verified run.")
        root = self.root / record["id"]
        index = checked_path(root, "bundle-manifest.json")
        if digest(index.read_bytes()) != record["bundle_sha256"]:
            fail("artifact_changed", "Frozen bundle manifest changed.")
        files = read_json(index)
        verify_files(root, files)
        actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
        if actual != set(files) | {"bundle-manifest.json"}:
            fail("artifact_changed", "Frozen bundle file inventory changed.")
        return root

    def _check_binding(self, record, binding):
        if binding != record["binding_sha256"]:
            fail("stale_binding", "Supply the binding SHA shown with the reviewed diff.")
        try:
            return self._bundle(record)
        except (OSError, ValueError, VerificationError):
            if record["state"] not in ("APPLIED", "REJECTED"):
                record.update(state="INVALIDATED", reason_code="artifact_changed")
                self._save(record, "invalidated", reason="artifact_changed")
            raise

    def _workspace(self, record, *, recovering=False):
        identity = record["identity"]
        repo = Path(identity["repository_path"])
        if str(repo.resolve()) != identity["repository_path"] or repo.is_symlink():
            fail("workspace_changed", "Repository path changed.")
        root = git(repo, "rev-parse", "--show-toplevel").stdout.decode("utf-8").strip()
        head = git(repo, "rev-parse", "HEAD").stdout.decode().strip()
        if Path(root).resolve() != repo or head != identity["base_commit"]:
            fail("workspace_changed", "Repository root or HEAD differs from the approved baseline.")
        raw = git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
        owned = set(record["changes"]) if recovering else set()
        temporary = set(self._temporary_paths(record)) if recovering else set()
        for entry in filter(None, raw.split(b"\0")):
            code, name = entry[:2].decode(), entry[3:].decode("utf-8")
            if not (code == " M" and name in owned or code == "??" and name in temporary):
                fail("workspace_changed", "Worktree or index has unrelated changes.")
        states = {}
        for name, change in record["changes"].items():
            path = checked_path(repo, name)
            if not path.is_file() or path.stat().st_nlink > 1:
                fail("workspace_changed", "Changed files must remain regular, unlinked files.")
            value = digest(path.read_bytes())
            if value == change["before"]:
                states[name] = "before"
            elif recovering and value == change["after"]:
                states[name] = "after"
            else:
                fail("workspace_changed", "Target file contents differ from the approved operation.")
        return states

    def request(self, run_directory, *, ttl_hours=24):
        if type(ttl_hours) is not int or not 1 <= ttl_hours <= 168:
            fail("invalid_expiry", "Approval lifetime must be 1 to 168 hours.")
        run = Path(run_directory).resolve()
        result = read_json(run / "result.json")
        verify_files(run, read_json(run / "manifest.json"))
        if result["status"] != "PASSED":
            fail("run_not_passed", "Only independently passed Agent runs can request approval.")
        task = TaskSpec.load(run / "task.json")
        final_dir = Path(result["final_verification_ref"]).resolve()
        if not final_dir.is_relative_to(run / "verification-runs"):
            fail("invalid_evidence", "Final verifier must belong to this Agent run.")
        final = read_json(final_dir / "result.json")
        verify_files(final_dir, read_json(final_dir / "manifest.json"))
        patch = (run / "changes.patch").read_bytes()
        if digest((final_dir / "changes.patch").read_bytes()) != digest(patch):
            fail("invalid_evidence", "Agent and final verification patches disagree.")
        selected = result["candidates"][-1]
        stages = ["baseline_regression", "candidate_regression", "candidate_feedback"]
        if task.holdout_dir:
            stages.append("candidate_holdout")
        if not (final["status"] == "PASSED" and final["purpose"] == "full"
                and final["task_id"] == result["task_id"] == task.task_id
                and final["base_commit"] == result["base_commit"] == task.base_commit
                and final["patch_sha256"] == selected["patch_sha256"] == digest(patch)
                and final["candidate_tree_sha256"] == selected["tree_sha256"]
                and all(final["stages"][name]["valid"] and final["stages"][name]["passed"] for name in stages)):
            fail("invalid_evidence", "Final verification identity or required checks disagree.")
        before, after = manifest(final_dir / "base"), manifest(final_dir / "candidate")
        if tree_hash(before) != final["base_tree_sha256"] or tree_hash(after) != final["candidate_tree_sha256"]:
            fail("artifact_changed", "Verified source snapshots changed.")
        policy = validate_patch(patch, task)
        changes = {name: {"before": before[name]["sha256"], "after": after[name]["sha256"]}
                   for name in before.keys() & after.keys() if before[name] != after[name]}
        if before.keys() != after.keys() or set(changes) != set(policy["paths"]):
            fail("unsupported_application", "Approval currently applies modifications to existing files only.")
        identity = {"run_id": result["run_id"], "task_id": task.task_id,
                    "model_mode": result["model_mode"], "repository_path": str(Path(task.repository_path).resolve()),
                    "base_commit": task.base_commit, "patch_sha256": digest(patch),
                    "candidate_tree_sha256": final["candidate_tree_sha256"],
                    "environment": final["environment"], "verification_plan_sha256": final["verification_plan_sha256"],
                    "policy_version": POLICY_VERSION,
                    "policy": {"editable_paths": list(task.editable_paths), "max_changed_files": task.max_changed_files,
                               "max_changed_lines": task.max_changed_lines},
                    "agent_result_sha256": digest((run / "result.json").read_bytes())}
        with repository_lock(identity["repository_path"]):
            with self._db() as db:
                existing = db.execute("SELECT data FROM approvals WHERE run_path=?", (str(run),)).fetchone()
            if existing:
                record = json.loads(existing[0])
                if record["identity"] != identity:
                    fail("run_changed", "The previously requested run identity changed.")
                self._bundle(record)
                return self.get(record["id"])
            record = {"id": "approval-" + uuid.uuid4().hex, "identity": identity, "changes": changes,
                      "created_at": self.clock(), "expires_at": self.clock() + ttl_hours * 3600,
                      "state": "WAITING_APPROVAL", "decision": None, "operation": {}}
            self._workspace(record)
            bundle = self.root / record["id"]
            bundle.mkdir()
            files = {}
            for label, source, inventory in (("base", final_dir / "base", before), ("candidate", final_dir / "candidate", after)):
                for name in inventory:
                    target = bundle / label / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    data = checked_path(source, name).read_bytes()
                    if digest(data) != inventory[name]["sha256"]:
                        fail("artifact_changed", "Source snapshot changed during freezing.")
                    target.write_bytes(data)
                    files[label + "/" + name] = {"sha256": digest(data)}
            for name, data in {"changes.patch": patch, "verification.json": canonical(final),
                               "task.json": canonical(task.to_dict())}.items():
                (bundle / name).write_bytes(data)
                files[name] = {"sha256": digest(data)}
            index = canonical(files)
            (bundle / "bundle-manifest.json").write_bytes(index)
            record["bundle_sha256"] = digest(index)
            record["binding_sha256"] = self._binding(record)
            self._workspace(record)
            with self._db() as db:
                db.execute("INSERT INTO approvals(id,run_path,data) VALUES(?,?,?)", (record["id"], str(run), json.dumps(record)))
                db.execute("INSERT INTO events(approval_id,timestamp,kind,details) VALUES(?,?,?,?)",
                           (record["id"], self.clock(), "requested", "{}"))
            return self.get(record["id"])

    def get(self, identity, *, include_diff=False):
        record = self._load(identity)
        record["expired"] = self.clock() >= record["expires_at"]
        if record["expired"] and record["state"] in ("WAITING_APPROVAL", "APPROVED"):
            record["state"] = "EXPIRED"
        if include_diff:
            record["diff"] = (self._bundle(record) / "changes.patch").read_text(encoding="utf-8")
        with self._db() as db:
            record["events"] = [{"timestamp": row[0], "kind": row[1], "details": json.loads(row[2])}
                                for row in db.execute("SELECT timestamp,kind,details FROM events WHERE approval_id=? ORDER BY sequence", (identity,))]
        return record

    def list(self):
        with self._db() as db:
            identities = [row[0] for row in db.execute("SELECT id FROM approvals ORDER BY rowid DESC")]
        return [self.get(identity) for identity in identities]

    def decide(self, identity, binding, decision, *, reviewer=None):
        if decision not in ("approve", "reject"):
            fail("invalid_decision", "Decision must be approve or reject.")
        record = self._load(identity)
        with repository_lock(record["identity"]["repository_path"]):
            record = self._load(identity)
            self._check_binding(record, binding)
            if record["decision"] is not None:
                if record["decision"]["value"] != decision:
                    fail("decision_conflict", "An incompatible decision is already recorded.")
                return self.get(identity)
            if record["state"] != "WAITING_APPROVAL":
                fail("invalid_state", "Approval is not waiting for a decision.")
            if self.clock() >= record["expires_at"]:
                record.update(state="EXPIRED")
                self._save(record, "expired")
                fail("approval_expired", "Create a new verified run after approval expiry.")
            if decision == "approve":
                try:
                    self._workspace(record)
                except VerificationError:
                    record.update(state="INVALIDATED", reason_code="workspace_changed")
                    self._save(record, "invalidated", reason="workspace_changed")
                    raise
            record["decision"] = {"value": decision, "reviewer": reviewer or getpass.getuser(), "timestamp": self.clock()}
            record["state"] = "APPROVED" if decision == "approve" else "REJECTED"
            self._save(record, "decided", **record["decision"])
            return self.get(identity)

    def apply(self, identity, binding, *, recover=False):
        record = self._load(identity)
        with repository_lock(record["identity"]["repository_path"]):
            record = self._load(identity)
            bundle = self._check_binding(record, binding)
            if record["state"] == "APPLIED":
                return self.get(identity)
            if not record["decision"] or record["decision"]["value"] != "approve":
                fail("not_approved", "Apply requires a recorded approval.")
            recovering = record["state"] in ("APPLYING", "RECOVERY_REQUIRED")
            if recovering and not recover:
                fail("recovery_required", "Use recover to reconcile an interrupted operation.")
            if not recovering and record["state"] != "APPROVED":
                fail("invalid_state", "Approval cannot be applied in its current state.")
            try:
                states = self._workspace(record, recovering=recovering)
                needs_write = any(value == "before" for value in states.values())
                if needs_write and self.clock() >= record["expires_at"]:
                    fail("approval_expired", "Expired approval cannot authorize further file writes.")
                policy = SimpleNamespace(**record["identity"]["policy"])
                if set(validate_patch((bundle / "changes.patch").read_bytes(), policy)["paths"]) != set(states):
                    fail("artifact_changed", "Patch policy and recorded file changes disagree.")
                if not recovering:
                    operation_id = "apply-" + uuid.uuid4().hex
                    record["operation"] = {"id": operation_id, "temporary_paths": [
                        str(Path(name).parent / (".patchtrail-" + operation_id + f"-{i}.tmp")).replace("\\", "/")
                        for i, name in enumerate(sorted(states))]}
                    record["state"] = "APPLYING"
                    self._save(record, "operation_prepared", operation_id=operation_id)
                for i, name in enumerate(sorted(states)):
                    # Recheck every file before each write, not just the next file.
                    states = self._workspace(record, recovering=True)
                    if states[name] == "after":
                        continue
                    if self.clock() >= record["expires_at"]:
                        fail("approval_expired", "Expired approval cannot authorize further file writes.")
                    self._replace_file(record, bundle, name, record["operation"]["temporary_paths"][i])
                    self._save(record, "file_applied", path=name)
                states = self._workspace(record, recovering=True)
                if not all(value == "after" for value in states.values()):
                    fail("recovery_required", "Operation did not reach the complete target tree.")
                if any(checked_path(Path(record["identity"]["repository_path"]), name).exists()
                       for name in self._temporary_paths(record)):
                    fail("recovery_required", "Unexpected temporary files need manual inspection.")
                record["state"] = "APPLIED"
                record.pop("reason_code", None)
                record["applied_at"] = self.clock()
                self._save(record, "applied", operation_id=record["operation"]["id"])
                return self.get(identity)
            except (VerificationError, OSError) as exc:
                stopped = "EXPIRED" if isinstance(exc, VerificationError) and exc.reason == "approval_expired" else "INVALIDATED"
                record.update(state="RECOVERY_REQUIRED" if record["operation"] else stopped,
                              reason_code=exc.reason if isinstance(exc, VerificationError) else "application_io_error")
                self._save(record, "application_stopped", reason=record["reason_code"])
                raise

    def _replace_file(self, record, bundle, name, temporary_name):
        repo = Path(record["identity"]["repository_path"])
        path, temporary = checked_path(repo, name), checked_path(repo, temporary_name)
        after = checked_path(bundle, "candidate/" + name).read_bytes()
        if digest(after) != record["changes"][name]["after"]:
            fail("artifact_changed", "Candidate changed immediately before replacement.")
        if temporary.exists():
            if digest(temporary.read_bytes()) != record["changes"][name]["after"]:
                fail("recovery_required", "Interrupted temporary file needs manual inspection.")
        else:
            with temporary.open("xb") as stream:
                stream.write(after)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        if digest(path.read_bytes()) != record["changes"][name]["before"]:
            fail("workspace_changed", "Target changed immediately before replacement.")
        os.replace(temporary, path)
