"""SQLite 管理事件、检查点、结果归档及显式长期记忆。"""

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import Field

from .contracts import EngineName, JsonObject, Message, Record, RunOutcome


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def now() -> str:
    return datetime.now(UTC).isoformat()


class Session(Record):
    id: str
    namespace: str
    snapshot_sha256: str
    status: str = "idle"
    state: JsonObject = Field(default_factory=dict)
    messages: list[Message] = Field(default_factory=list)
    summary: str = ""
    pending_input: str | None = None
    revision: int = 0
    engine: EngineName = "native"


class Memory(Record):
    key: str
    value: str
    kind: str = "preference"
    source: str
    updated_at: str
    version: int
    deleted: bool = False


class RuntimeStore:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, namespace TEXT NOT NULL,
                    snapshot_sha256 TEXT NOT NULL, status TEXT NOT NULL,
                    state TEXT NOT NULL, messages TEXT NOT NULL, summary TEXT NOT NULL,
                    pending_input TEXT, revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL, run_id TEXT, type TEXT NOT NULL,
                    payload TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY, namespace TEXT NOT NULL,
                    session_id TEXT NOT NULL, content TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memories (
                    namespace TEXT NOT NULL, key TEXT NOT NULL, version INTEGER NOT NULL,
                    value TEXT NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL,
                    updated_at TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(namespace,key,version)
                );
            """)
            # 旧会话默认属于原生版；只补元数据，不改写其检查点。
            db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(sessions)")}
            if "engine" not in columns:
                db.execute("ALTER TABLE sessions ADD COLUMN engine TEXT NOT NULL DEFAULT 'native'")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def create_session(
        self, namespace: str, snapshot_sha256: str, engine: EngineName = "native"
    ) -> Session:
        if not namespace.strip():
            raise ValueError("namespace 不能为空")
        session = Session(
            id=uuid4().hex, namespace=namespace, snapshot_sha256=snapshot_sha256, engine=engine
        )
        with self.connect() as db:
            db.execute(
                "INSERT INTO sessions "
                "(id,namespace,snapshot_sha256,status,state,messages,summary,pending_input,revision,engine) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (session.id, namespace, snapshot_sha256, "idle", "{}", "[]", "", None, 0, engine),
            )
            self._event(
                db,
                session.id,
                None,
                "session.created",
                {"namespace": namespace, "snapshot_sha256": snapshot_sha256, "engine": engine},
            )
        return session

    def load_session(self, session_id: str, namespace: str) -> Session:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM sessions WHERE id=? AND namespace=?", (session_id, namespace)
            ).fetchone()
        if row is None:
            raise ValueError("会话不存在或不属于当前 namespace")
        return Session(
            id=row["id"],
            namespace=row["namespace"],
            snapshot_sha256=row["snapshot_sha256"],
            status=row["status"],
            state=json.loads(row["state"]),
            messages=[Message.model_validate(m) for m in json.loads(row["messages"])],
            summary=row["summary"],
            pending_input=row["pending_input"],
            revision=row["revision"],
            engine=row["engine"],
        )

    @staticmethod
    def _event(
        db: sqlite3.Connection, session_id: str, run_id: str | None, kind: str, payload: JsonObject
    ) -> None:
        db.execute(
            "INSERT INTO events(session_id,run_id,type,payload,created_at) VALUES (?,?,?,?,?)",
            (session_id, run_id, kind, encode(payload), now()),
        )

    def append_event(self, session_id: str, run_id: str, kind: str, payload: JsonObject) -> None:
        with self.connect() as db:
            self._event(db, session_id, run_id, kind, payload)

    def begin_run(self, session: Session, user_input: str) -> str:
        run_id = uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE sessions SET status='running',pending_input=?,revision=revision+1 "
                "WHERE id=? AND namespace=? AND revision=? AND status!='running'",
                (user_input, session.id, session.namespace, session.revision),
            ).rowcount
            if changed != 1:
                raise ValueError("会话正在运行或已被其他请求更新")
            turn_id = run_id
            if session.pending_input is not None:
                previous = db.execute(
                    "SELECT run_id,payload FROM events WHERE session_id=? AND type='run.started' "
                    "ORDER BY seq DESC LIMIT 1",
                    (session.id,),
                ).fetchone()
                if previous:
                    turn_id = json.loads(previous["payload"]).get("turn_id", previous["run_id"])
            self._event(
                db,
                session.id,
                run_id,
                "run.started",
                {"input": user_input, "turn_id": turn_id, "engine": session.engine},
            )
        return run_id

    def turn_id(self, session_id: str, run_id: str) -> str:
        with self.connect() as db:
            row = db.execute(
                "SELECT payload FROM events WHERE session_id=? AND run_id=? AND type='run.started'",
                (session_id, run_id),
            ).fetchone()
        if row is None:
            raise ValueError("运行不存在")
        return str(json.loads(row["payload"])["turn_id"])

    def commit_run(self, session: Session, run_id: str, result: RunOutcome) -> None:
        # 回答与状态同一事务提交；模型/工具事件只描述轨迹，不构成恢复点。
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE sessions SET status=?,state=?,messages=?,summary=?,pending_input=NULL "
                "WHERE id=? AND revision=? AND status='running'",
                (
                    result.status,
                    encode(result.task_state),
                    encode([m.model_dump(mode="json") for m in result.messages]),
                    result.summary,
                    session.id,
                    session.revision + 1,
                ),
            ).rowcount
            if changed != 1:
                raise ValueError("运行所有权已改变，拒绝提交")
            self._event(db, session.id, run_id, "run.finished", result.model_dump(mode="json"))

    def fail_run(self, session: Session, run_id: str, status: str, message: str) -> None:
        with self.connect() as db:
            changed = db.execute(
                "UPDATE sessions SET status=? WHERE id=? AND revision=? AND status='running'",
                (status, session.id, session.revision + 1),
            ).rowcount
            if changed != 1:
                raise ValueError("运行所有权已改变，拒绝更新终态")
            self._event(db, session.id, run_id, "run." + status, {"message": message})

    def recover_interrupted(self, session_id: str, namespace: str) -> None:
        # 显式操作：操作者应先确认旧进程已停止，不能自动抢占仍在工作的实例。
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE sessions SET status='interrupted',revision=revision+1 "
                "WHERE id=? AND namespace=? AND status='running'",
                (session_id, namespace),
            ).rowcount
            if changed != 1:
                raise ValueError("只有 running 会话需要标记中断")
            self._event(db, session_id, None, "run.interrupted", {"reason": "explicit_recovery"})

    def events(self, session_id: str, namespace: str) -> list[JsonObject]:
        self.load_session(session_id, namespace)
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM events WHERE session_id=? ORDER BY seq", (session_id,)
            ).fetchall()
        return [
            {
                "seq": r["seq"],
                "run_id": r["run_id"],
                "type": r["type"],
                "payload": json.loads(r["payload"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def archive(self, namespace: str, session_id: str, content: str) -> str:
        identifier = hashlib.sha256(
            (namespace + "\0" + session_id + "\0" + content).encode("utf-8")
        ).hexdigest()
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO artifacts VALUES (?,?,?,?)",
                (identifier, namespace, session_id, content),
            )
        return identifier

    def read_artifact(
        self,
        namespace: str,
        session_id: str,
        identifier: str,
        offset: int = 0,
        limit: int = 4000,
        *,
        max_result_chars: int | None = None,
    ) -> JsonObject:
        if offset < 0 or not 1 <= limit <= 8000:
            raise ValueError("offset 必须非负，limit 必须在 1—8000 之间")
        with self.connect() as db:
            row = db.execute(
                "SELECT content FROM artifacts WHERE id=? AND namespace=? AND session_id=?",
                (identifier, namespace, session_id),
            ).fetchone()
        if row is None:
            raise ValueError("结果不存在或不属于当前会话")
        content = row["content"]

        def page(size: int) -> JsonObject:
            text = content[offset : offset + size]
            end = offset + len(text)
            more = end < len(content)
            return {
                "artifact_id": identifier,
                "content": text,
                "offset": offset,
                "total_chars": len(content),
                "has_more": more,
                "next_offset": end if more else None,
            }

        maximum = min(limit, max(0, len(content) - offset))
        result = page(maximum)
        if max_result_chars is None or len(encode(result)) <= max_result_chars:
            return result
        # 按完整 JSON 计量，包含引号、反斜杠转义及分页元数据；不能只裁正文。
        low, high = 0, maximum
        while low < high:
            middle = (low + high + 1) // 2
            if len(encode(page(middle))) <= max_result_chars:
                low = middle
            else:
                high = middle - 1
        result = page(low)
        if len(encode(result)) > max_result_chars or (maximum > 0 and low == 0):
            raise ValueError("分页结果预算不足，无法返回内容及分页元数据")
        return result

    def save_memory(
        self, namespace: str, key: str, value: str, source: str, deleted: bool = False
    ) -> Memory:
        if not namespace.strip() or not key.strip() or not source.strip():
            raise ValueError("记忆的 namespace、key、source 不能为空")
        if len(key) > 100 or len(value) > 2000 or (not deleted and not value.strip()):
            raise ValueError("记忆内容为空或超过长度限制")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM memories WHERE namespace=? AND key=?",
                (namespace, key),
            ).fetchone()[0]
            item = Memory(
                key=key,
                value=value,
                source=source,
                updated_at=now(),
                version=version,
                deleted=deleted,
            )
            db.execute(
                "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?)",
                (namespace, key, version, value, item.kind, source, item.updated_at, int(deleted)),
            )
        return item

    def memories(self, namespace: str) -> list[Memory]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT m.* FROM memories m JOIN "
                "(SELECT key,MAX(version) AS version FROM memories WHERE namespace=? GROUP BY key) h "
                "ON m.key=h.key AND m.version=h.version WHERE m.namespace=? AND m.deleted=0 "
                "ORDER BY m.updated_at DESC,m.key",
                (namespace, namespace),
            ).fetchall()
        return [
            Memory(
                key=r["key"],
                value=r["value"],
                kind=r["kind"],
                source=r["source"],
                updated_at=r["updated_at"],
                version=r["version"],
            )
            for r in rows
        ]
