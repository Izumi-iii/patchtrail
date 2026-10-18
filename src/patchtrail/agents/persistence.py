"""Synchronous, local SQLite checkpointer and write-ahead node receipts.

Full checkpoints suit this bounded, sequential graph; async graphs, DeltaChannel,
remote stores and time travel are deliberately outside this adapter's contract.
"""

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from langgraph.checkpoint.base import (
    BaseCheckpointSaver, CheckpointTuple, WRITES_IDX_MAP, get_checkpoint_id, get_checkpoint_metadata,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from patchtrail.domain.task import VerificationError


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sha(data):
    return hashlib.sha256(data).hexdigest()


@contextmanager
def run_lock(directory):
    path = Path(directory) / ".execution.lock"
    if path.is_symlink() or path.exists() and (path.stat().st_nlink > 1 or getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
        raise VerificationError("RECOVERY_REQUIRED", "unsafe_run_lock", "Unsafe execution lock path.")
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
            raise VerificationError("RECOVERY_REQUIRED", "run_busy", "Another process owns this Agent run.") from None
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class LocalCheckpointSaver(BaseCheckpointSaver):
    """One database per run. Connections are short-lived and safe across threads."""

    def __init__(self, path):
        super().__init__(serde=JsonPlusSerializer(pickle_fallback=False, allowed_json_modules=None,
                                                 allowed_msgpack_modules=None))
        self.path = Path(path)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS checkpoints (
                    thread TEXT, ns TEXT, id TEXT, parent TEXT, type TEXT, data BLOB,
                    meta_type TEXT, meta BLOB, sha TEXT, PRIMARY KEY(thread, ns, id));
                CREATE TABLE IF NOT EXISTS writes (
                    thread TEXT, ns TEXT, id TEXT, task TEXT, idx INTEGER, channel TEXT,
                    type TEXT, data BLOB, path TEXT, sha TEXT, PRIMARY KEY(thread,ns,id,task,idx));
                CREATE TABLE IF NOT EXISTS control (key TEXT PRIMARY KEY, data BLOB, sha TEXT);
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=20)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def check(data, expected):
        if sha(data) != expected:
            raise VerificationError("RECOVERY_REQUIRED", "checkpoint_changed", "Checkpoint contents changed.")

    def save_control(self, key, value):
        data = encoded(value)
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO control VALUES(?,?,?)", (key, data, sha(data)))

    def control(self, key):
        with self.db() as db:
            row = db.execute("SELECT data,sha FROM control WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        self.check(row[0], row[1])
        return json.loads(row[0])

    def get_tuple(self, config):
        options = config["configurable"]
        thread, ns = options["thread_id"], options.get("checkpoint_ns", "")
        with self.db() as db:
            identity = get_checkpoint_id(config)
            if not identity:
                row = db.execute("SELECT id FROM checkpoints WHERE thread=? AND ns=? ORDER BY id DESC LIMIT 1",
                                 (thread, ns)).fetchone()
                if row is None:
                    return None
                identity = row[0]
            row = db.execute("SELECT parent,type,data,meta_type,meta,sha FROM checkpoints WHERE thread=? AND ns=? AND id=?",
                             (thread, ns, identity)).fetchone()
            if row is None:
                return None
            self.check(row[2] + row[4], row[5])
            pending = []
            for task, channel, kind, data, expected in db.execute(
                    "SELECT task,channel,type,data,sha FROM writes WHERE thread=? AND ns=? AND id=? ORDER BY path,task,idx",
                    (thread, ns, identity)):
                self.check(data, expected)
                pending.append((task, channel, self.serde.loads_typed((kind, data))))
        def cfg(checkpoint_id):
            return {"configurable": {"thread_id": thread, "checkpoint_ns": ns, "checkpoint_id": checkpoint_id}}
        return CheckpointTuple(cfg(identity), self.serde.loads_typed((row[1], row[2])),
                               self.serde.loads_typed((row[3], row[4])), cfg(row[0]) if row[0] else None, pending)

    def put(self, config, checkpoint, metadata, new_versions):
        options = config["configurable"]
        thread, ns = options["thread_id"], options.get("checkpoint_ns", "")
        kind, data = self.serde.dumps_typed(checkpoint)
        meta_kind, meta = self.serde.dumps_typed(get_checkpoint_metadata(config, metadata))
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO checkpoints VALUES(?,?,?,?,?,?,?,?,?)",
                       (thread, ns, checkpoint["id"], options.get("checkpoint_id"), kind, data, meta_kind, meta, sha(data + meta)))
        return {"configurable": {"thread_id": thread, "checkpoint_ns": ns, "checkpoint_id": checkpoint["id"]}}

    def put_writes(self, config, writes, task_id, task_path=""):
        options = config["configurable"]
        with self.db() as db:
            for index, (channel, value) in enumerate(writes):
                index = WRITES_IDX_MAP.get(channel, index)
                kind, data = self.serde.dumps_typed(value)
                action = "REPLACE" if index < 0 else "IGNORE"
                db.execute(f"INSERT OR {action} INTO writes VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (options["thread_id"], options.get("checkpoint_ns", ""), options["checkpoint_id"],
                            task_id, index, channel, kind, data, task_path, sha(data)))

    def list(self, config, *, filter=None, before=None, limit=None):
        with self.db() as db:
            rows = db.execute("SELECT thread,ns,id FROM checkpoints ORDER BY id DESC").fetchall()
        yielded = 0
        for thread, ns, identity in rows:
            options = config["configurable"] if config else {}
            if config and (thread != options["thread_id"] or ns != options.get("checkpoint_ns", "")):
                continue
            if before and identity >= get_checkpoint_id(before):
                continue
            value = self.get_tuple({"configurable": {"thread_id": thread, "checkpoint_ns": ns, "checkpoint_id": identity}})
            if filter and any(value.metadata.get(key) != item for key, item in filter.items()):
                continue
            if limit is not None and yielded >= limit:
                break
            yielded += 1
            yield value
