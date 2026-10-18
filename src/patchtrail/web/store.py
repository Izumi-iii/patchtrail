"""SQLite conversation persistence, independent of the HTTP transport."""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    repository_path TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id)
                        ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_conversation
                    ON messages(conversation_id, created_at);
                CREATE TABLE IF NOT EXISTS submissions (
                    conversation_id TEXT NOT NULL REFERENCES conversations(id)
                        ON DELETE CASCADE,
                    request_id TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    PRIMARY KEY (conversation_id, request_id)
                );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(conversations)")}
            for name, default in (("task_source", "demo"), ("task_path", "")):
                if name not in columns:
                    db.execute(f"ALTER TABLE conversations ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self):
        item = dict(id=uuid.uuid4().hex, title="新会话", repository_path="",
                    created_at=now(), updated_at=now())
        with self.connect() as db:
            db.execute("INSERT INTO conversations(id,title,repository_path,created_at,updated_at) VALUES (:id,:title,:repository_path,"
                       ":created_at,:updated_at)", item)
        return self.get(item["id"])

    def list(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("""
                SELECT c.*, (SELECT COUNT(*) FROM messages m
                    WHERE m.conversation_id=c.id) AS message_count
                FROM conversations c ORDER BY c.updated_at DESC, c.id
            """)]

    def get(self, conversation_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM conversations WHERE id=?",
                             (conversation_id,)).fetchone()
            if row is None:
                raise KeyError(conversation_id)
            messages = []
            for message in db.execute("SELECT * FROM messages WHERE conversation_id=? "
                                      "ORDER BY created_at, rowid", (conversation_id,)):
                item = dict(message)
                item["metadata"] = json.loads(item["metadata"])
                messages.append(item)
            return {**dict(row), "messages": messages, "message_count": len(messages)}

    def update(self, conversation_id, changes):
        self.get(conversation_id)
        if changes:
            fields = list(changes)
            with self.connect() as db:
                db.execute("UPDATE conversations SET "
                           + ",".join(f"{field}=?" for field in fields)
                           + ", updated_at=? WHERE id=?",
                           [changes[field] for field in fields] + [now(), conversation_id])
        return self.get(conversation_id)

    def delete(self, conversation_id):
        with self.connect() as db:
            result = db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
            if not result.rowcount:
                raise KeyError(conversation_id)

    def submission_hash(self, conversation_id, request_id):
        with self.connect() as db:
            row = db.execute("SELECT input_hash FROM submissions WHERE conversation_id=? "
                             "AND request_id=?", (conversation_id, request_id)).fetchone()
            return row[0] if row else None

    def append_exchange(self, conversation_id, request_id, input_hash, content, answer,
                        metadata, repository_path):
        with self.connect() as db:
            row = db.execute("SELECT title FROM conversations WHERE id=?",
                             (conversation_id,)).fetchone()
            if row is None:
                raise KeyError(conversation_id)
            db.execute("INSERT INTO submissions VALUES (?,?,?)",
                       (conversation_id, request_id, input_hash))
            for role, text, extra in (("user", content, {}), ("assistant", answer, metadata)):
                db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?)",
                           (uuid.uuid4().hex, conversation_id, role, text,
                            json.dumps(extra, ensure_ascii=False), now()))
            title = content.replace("\n", " ")[:32] if row[0] == "新会话" else row[0]
            db.execute("UPDATE conversations SET title=?, repository_path=?, updated_at=? "
                       "WHERE id=?", (title, repository_path, now(), conversation_id))
        return self.get(conversation_id)
