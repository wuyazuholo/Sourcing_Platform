"""受控 JSON 数组导入与显式演示数据；独占发布，禁止覆盖已发布快照。"""

import json
import os
import sqlite3
import tempfile
from collections.abc import Iterable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from sourcing_agent.sourcing.models import TradeRecord


def publish_snapshot(
    path: Path,
    records: Iterable[TradeRecord],
    scope: str,
    *,
    allow_empty: bool = False,
    extra_metadata: dict[str, str] | None = None,
) -> int:
    if not scope.strip():
        raise ValueError("必须说明数据范围")
    if {"schema_version", "scope", "coverage", "created_at", "row_count"} & (
        extra_metadata or {}
    ).keys():
        raise ValueError("额外元数据不能覆盖快照格式、范围或记录数")
    path = path.resolve()
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(suffix=".sqlite", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with closing(sqlite3.connect(temporary)) as db, db:
            db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            db.execute("""CREATE TABLE trades(
                record_id INTEGER NOT NULL,source_table TEXT NOT NULL,shipment_date TEXT,
                supplier_id INTEGER,supplier_name TEXT,supplier_country TEXT,
                buyer_id INTEGER,buyer_name TEXT,buyer_country TEXT,hs_code TEXT,
                goods_description TEXT,cif_usd REAL,source_file_date TEXT,
                PRIMARY KEY(source_table,record_id))""")
            count = 0
            for record in records:
                if record.source_table not in {"dbo.panjivaCOImport", "dbo.panjivaMXExport"}:
                    raise ValueError("只支持已审核的墨西哥出口或历史哥伦比亚进口快照口径")
                db.execute(
                    "INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    tuple(record.model_dump(mode="json").values()),
                )
                count += 1
            if not count and not allow_empty:
                raise ValueError("不能发布空快照")
            db.executemany(
                "INSERT INTO metadata VALUES(?,?)",
                [
                    ("schema_version", "1"),
                    ("scope", scope),
                    ("coverage", "bounded_extract_not_complete_market"),
                    ("created_at", datetime.now(UTC).isoformat()),
                    ("row_count", str(count)),
                ],
            )
            db.executemany("INSERT INTO metadata VALUES(?,?)", (extra_metadata or {}).items())
            db.execute("CREATE INDEX trade_hs_date ON trades(hs_code,shipment_date)")
            db.execute("CREATE INDEX trade_supplier_date ON trades(supplier_id,shipment_date)")
        if os.name == "nt":
            os.rename(temporary, path)
        else:
            os.link(temporary, path)
        return count
    finally:
        temporary.unlink(missing_ok=True)


def read_records(path: Path) -> list[TradeRecord]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, list):
        raise ValueError("输入必须是 JSON 数组")
    return [TradeRecord.model_validate(row) for row in payload]


def demo_records() -> list[TradeRecord]:
    base = dict(
        shipment_date="2025-10-01",
        supplier_id=10,
        supplier_name="Demo Supplier A",
        supplier_country="United States",
        buyer_id=100,
        buyer_name="Demo Buyer A",
        buyer_country="Colombia",
        hs_code="8418910000",
        goods_description="PARTES REFRIGERACION",
        cif_usd=1000,
    )
    return [
        TradeRecord.model_validate({**base, **patch})
        for patch in [
            {"record_id": 1},
            {"record_id": 2, "buyer_id": 101, "buyer_name": "Demo Buyer B"},
            {
                "record_id": 3,
                "supplier_id": 20,
                "supplier_name": "Demo Supplier B",
                "supplier_country": "China",
                "shipment_date": "2025-10-02",
                "buyer_id": None,
            },
            {"record_id": 4, "hs_code": "8539100000"},
            {"record_id": 5, "shipment_date": "2024-10-01"},
            {"record_id": 6, "supplier_id": None},
        ]
    ]
