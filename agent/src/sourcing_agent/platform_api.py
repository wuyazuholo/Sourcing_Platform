"""Loopback HTTP bridge: authenticated, owner-scoped asynchronous Mexico searches."""

import hmac
import json
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Full, Queue
from threading import Thread
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from pydantic import Field, ValidationError

from sourcing_agent.adapters.sqlserver import extraction_query
from sourcing_agent.database_config import DatabaseConfig
from sourcing_agent.harness.contracts import EngineName, Record
from sourcing_agent.sourcing.database_search import search_database
from sourcing_agent.sourcing.models import Requirements


class SearchRequest(Record):
    owner: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    requirements: Requirements
    engine: EngineName = "native"
    max_records: int = Field(default=5000, ge=1, le=50000, strict=True)
    scan_limit: int = Field(default=200000, ge=0, le=2000000, strict=True)


def public_report(value):
    """Preserve BIGINTs in browsers; internal filesystem paths never cross the bridge."""
    if isinstance(value, dict):
        return {
            key: str(item)
            if key in {"supplier_id", "buyer_id", "record_id"} and item is not None
            else public_report(item)
            for key, item in value.items()
            if key not in {"snapshot", "state_dir"}
        }
    if isinstance(value, list):
        return [public_report(item) for item in value]
    return value


class SearchJobs:
    def __init__(self, config: DatabaseConfig, directory: Path, capacity: int = 8) -> None:
        self.config = config
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "jobs.sqlite"
        self.queue = Queue(maxsize=capacity)
        with closing(self.connect()) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,owner TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,request TEXT NOT NULL,result TEXT,error TEXT)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS jobs_owner ON jobs(owner,id)")
            db.execute(
                "UPDATE jobs SET status='failed',error=? WHERE status IN ('queued','running')",
                ("服务重启中断了查询，请重新提交。",),
            )
        self.worker = Thread(target=self._work, name="sourcing-search", daemon=True)
        self.worker.start()

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def submit(self, request: SearchRequest) -> dict:
        extraction_query(request.requirements, request.max_records, request.scan_limit)
        identifier = uuid4().hex
        timestamp = datetime.now(UTC).isoformat()
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT INTO jobs(id,owner,status,created_at,updated_at,request) VALUES(?,?,'queued',?,?,?)",
                (identifier, request.owner, timestamp, timestamp, request.model_dump_json()),
            )
        try:
            self.queue.put_nowait((identifier, request))
        except Full:
            with closing(self.connect()) as db, db:
                db.execute("DELETE FROM jobs WHERE id=?", (identifier,))
            raise
        return self.get(identifier, request.owner)

    def get(self, identifier: str, owner: str) -> dict | None:
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT id,status,created_at,updated_at,result,error FROM jobs WHERE id=? AND owner=?",
                (identifier, owner),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["result"] = json.loads(result["result"]) if result["result"] else None
        return result

    def _update(self, identifier: str, status: str, result=None, error=None):
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE jobs SET status=?,updated_at=?,result=?,error=? WHERE id=?",
                (
                    status,
                    datetime.now(UTC).isoformat(),
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    error,
                    identifier,
                ),
            )

    def _work(self):
        while True:
            item = self.queue.get()
            try:
                if item is None:
                    return
                identifier, request = item
                self._update(identifier, "running")
                try:
                    report = search_database(
                        self.config,
                        request.requirements,
                        self.directory / "runs",
                        max_records=request.max_records,
                        scan_limit=request.scan_limit,
                        engine=request.engine,
                    )
                    if not report["ok"]:
                        raise RuntimeError("Incomplete investigation")
                    self._update(identifier, "completed", public_report(report))
                except Exception:
                    # Driver, filesystem and validation exceptions may contain private row data.
                    self._update(
                        identifier,
                        "failed",
                        error="调查未完成，可能是数据库连接、查询超时或数据校验失败；这不表示没有匹配记录。请稍后重试或缩小范围。",
                    )
            finally:
                self.queue.task_done()

    def close(self):
        # Drain accepted jobs before shutdown, so they cannot be left falsely queued.
        self.queue.put(None)
        self.worker.join()


class PlatformServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, token: str, config: DatabaseConfig, directory: Path):
        if len(token) < 32:
            raise ValueError("SOURCING_AGENT_API_TOKEN 至少需要 32 个字符")
        self.token = token
        super().__init__(("127.0.0.1", port), PlatformHandler)
        try:
            self.jobs = SearchJobs(config, directory)
        except Exception:
            super().server_close()
            raise

    def server_close(self):
        super().server_close()
        self.jobs.close()


class PlatformHandler(BaseHTTPRequestHandler):
    server_version = "SourcingBridge"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        # Request URLs/headers/bodies never become application logs.
        pass

    def reply(self, status: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        actual = self.headers.get("Authorization", "").encode("utf-8")
        expected = ("Bearer " + self.server.token).encode("utf-8")
        if not hmac.compare_digest(actual, expected):
            self.close_connection = True
            self.reply(401, {"message": "服务鉴权失败"})
            return False
        return True

    def do_GET(self):
        if not self.authorized():
            return
        parts = urlsplit(self.path)
        if parts.path == "/health" and not parts.query:
            self.reply(
                200, {"ready": True, "dataset": "Mexico exports", "table": "dbo.panjivaMXExport"}
            )
            return
        match = re.fullmatch(r"/v1/searches/([a-f0-9]{32})", parts.path)
        params = parse_qs(parts.query)
        if not match or set(params) != {"owner"} or len(params["owner"]) != 1:
            self.reply(404, {"message": "调查不存在"})
            return
        job = self.server.jobs.get(match[1], params["owner"][0])
        self.reply(200 if job else 404, job or {"message": "调查不存在"})

    def do_POST(self):
        if not self.authorized():
            return
        if self.path != "/v1/searches":
            self.reply(404, {"message": "接口不存在"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if (
                not 0 < length <= 64000
                or self.headers.get_content_type() != "application/json"
                or self.headers.get("Transfer-Encoding")
            ):
                self.close_connection = True
                self.reply(400, {"message": "请求格式不正确或过大"})
                return
            request = SearchRequest.model_validate_json(self.rfile.read(length))
            job = self.server.jobs.submit(request)
        except (ValueError, ValidationError):
            self.reply(400, {"message": "请检查 HS/商品、起止日期、排序和查询上限"})
            return
        except Full:
            self.reply(429, {"message": "调查队列已满，请稍后重试"})
            return
        except Exception:
            self.reply(503, {"message": "暂时无法提交调查，请稍后重试"})
            return
        self.reply(202, job)


def serve_platform(port: int, token: str, config: DatabaseConfig, directory: Path):
    if not 1 <= port <= 65535:
        raise ValueError("HTTP 端口必须在 1—65535 之间")
    with PlatformServer(port, token, config, directory) as server:
        print(f"Agent HTTP service: http://127.0.0.1:{port} (Mexico exports)", flush=True)
        server.serve_forever()
