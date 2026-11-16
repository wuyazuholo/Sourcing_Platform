"""读取与旧项目兼容的 schema_version=1 SQLite 快照；模型不能提供 SQL。"""

import hashlib
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sourcing_agent.sourcing.models import (
    BuyerActivity,
    Candidate,
    EvidenceSource,
    MonthActivity,
    ProductActivity,
    ProductMatch,
    Requirements,
    SupplierProfile,
    TradeEvidence,
)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def literal_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def text_contains(value: str | None, needle: str) -> int:
    return int(value is not None and needle.casefold() in value.casefold())


def hs_matches(value: str | None, prefix: str) -> int:
    return int(bool(value and re.search(r"(?<![0-9])" + re.escape(prefix), value.replace(".", ""))))


class TradeRepository:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve(strict=True)
        self.sha256 = digest(self.path)
        with self.connect() as db:
            self.metadata = {
                str(r["key"]): str(r["value"]) for r in db.execute("SELECT key,value FROM metadata")
            }
            if self.metadata.get("schema_version") != "1":
                raise ValueError("只支持 schema_version=1 交易快照")
            row = db.execute("SELECT MIN(shipment_date),MAX(shipment_date) FROM trades").fetchone()
            self.start_date, self.end_date = row[0], row[1]

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        db.create_function("hs_matches", 2, hs_matches, deterministic=True)
        db.create_function("text_contains", 2, text_contains, deterministic=True)
        db.execute("PRAGMA query_only=ON")
        remaining = 10000

        def stop() -> int:
            nonlocal remaining
            remaining -= 1
            return int(remaining <= 0)

        db.set_progress_handler(stop, 10000)
        try:
            yield db
        finally:
            db.close()

    def verify(self) -> None:
        if digest(self.path) != self.sha256:
            raise ValueError("交易快照已变化，请重新装配并创建新会话")

    def filters(self, req: Requirements) -> tuple[str, list[object]]:
        if missing := req.missing():
            raise ValueError("请补充：" + "、".join(missing))
        clauses = [
            "supplier_id IS NOT NULL",
            "supplier_name IS NOT NULL",
            "TRIM(supplier_name)!=''",
            "shipment_date BETWEEN ? AND ?",
        ]
        assert req.start_date and req.end_date
        parameters: list[object] = [req.start_date.isoformat(), req.end_date.isoformat()]
        for column, value in (
            ("supplier_country", req.supplier_country),
            ("buyer_country", req.market_country),
        ):
            if value:
                clauses.append(column + "=? COLLATE NOCASE")
                parameters.append(value)
        if req.hs_prefix:
            clauses.append("hs_matches(hs_code,?)=1")
            parameters.append(req.hs_prefix)
        if req.product_text:
            clauses.append("text_contains(goods_description,?)=1")
            parameters.append(req.product_text)
        if req.exclude_supplier_id:
            clauses.append("supplier_id!=?")
            parameters.append(req.exclude_supplier_id)
        return " AND ".join(clauses), parameters

    def find_candidates(self, req: Requirements) -> list[Candidate]:
        where, params = self.filters(req)
        ordering = {
            "recent": "latest_date DESC,active_months DESC,record_count DESC,buyer_count DESC",
            "customer_coverage": "buyer_count DESC,active_months DESC,record_count DESC,latest_date DESC",
            "activity": "active_months DESC,record_count DESC,buyer_count DESC,latest_date DESC",
        }[req.ranking]
        with self.connect() as db:
            rows = db.execute(
                f"""SELECT supplier_id,MAX(supplier_name) AS supplier_name,
                    CASE WHEN COUNT(DISTINCT supplier_country)=1
                         THEN MAX(supplier_country) ELSE NULL END AS supplier_country,
                    COUNT(*) AS record_count,COUNT(DISTINCT buyer_id) AS buyer_count,
                    COUNT(DISTINCT SUBSTR(shipment_date,1,7)) AS active_months,
                    MAX(shipment_date) AS latest_date FROM trades WHERE {where}
                    GROUP BY supplier_id ORDER BY {ordering},supplier_id ASC LIMIT ?""",
                [*params, req.limit],
            ).fetchall()
        return [Candidate.model_validate(dict(row)) for row in rows]

    def evidence(self, req: Requirements, supplier_id: int) -> list[TradeEvidence]:
        where, params = self.filters(req)
        with self.connect() as db:
            rows = db.execute(
                f"SELECT * FROM trades WHERE {where} AND supplier_id=? "
                "ORDER BY shipment_date DESC,record_id DESC LIMIT 3",
                [*params, supplier_id],
            ).fetchall()
        evidence = []
        for row in rows:
            source = EvidenceSource.model_validate({**dict(row), "snapshot_sha256": self.sha256})
            evidence.append(
                TradeEvidence(
                    id=f"trade:{self.sha256}:{source.source_table}:{source.record_id}",
                    title=f"交易记录 {source.record_id}",
                    text=f"{source.shipment_date}；发货方：{source.supplier_name}；"
                    f"收货方：{source.buyer_name}；HS：{source.hs_code}；商品：{source.goods_description}",
                    source=source,
                )
            )
        return evidence

    def profile(self, req: Requirements, supplier_id: int) -> SupplierProfile:
        where, params = self.filters(req)
        where += " AND supplier_id=?"
        params.append(supplier_id)
        with self.connect() as db:
            monthly = db.execute(
                f"SELECT SUBSTR(shipment_date,1,7) AS month,COUNT(*) AS record_count "
                f"FROM trades WHERE {where} GROUP BY month ORDER BY month",
                params,
            ).fetchall()
            buyers = db.execute(
                f"SELECT buyer_id,MAX(buyer_name) AS buyer_name,COUNT(*) AS record_count "
                f"FROM trades WHERE {where} AND buyer_id IS NOT NULL GROUP BY buyer_id "
                "ORDER BY record_count DESC,buyer_id LIMIT 5",
                params,
            ).fetchall()
            products = db.execute(
                f"SELECT hs_code,COUNT(*) AS record_count FROM trades WHERE {where} "
                "GROUP BY hs_code ORDER BY record_count DESC,hs_code LIMIT 5",
                params,
            ).fetchall()
            counts = db.execute(
                f"SELECT COUNT(*),COALESCE(SUM(buyer_id IS NULL),0) FROM trades WHERE {where}",
                params,
            ).fetchone()
        return SupplierProfile(
            supplier_id=supplier_id,
            monthly_activity=[MonthActivity(**dict(r)) for r in monthly],
            buyers=[BuyerActivity(**dict(r)) for r in buyers],
            products=[ProductActivity(**dict(r)) for r in products],
            record_count=counts[0],
            missing_buyer_ids=counts[1],
        )

    def search_products(self, text: str) -> list[ProductMatch]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT hs_code,MIN(goods_description) AS description,COUNT(*) AS record_count "
                "FROM trades WHERE goods_description LIKE ? ESCAPE '\\' "
                "OR hs_code LIKE ? ESCAPE '\\' GROUP BY hs_code ORDER BY record_count DESC,hs_code LIMIT 10",
                ["%" + literal_like(text) + "%", literal_like(text) + "%"],
            ).fetchall()
        return [ProductMatch.model_validate(dict(row)) for row in rows]
