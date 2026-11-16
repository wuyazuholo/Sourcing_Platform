"""Bounded, parameterized SQL Server reads; no model-provided SQL or database writes."""

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import date, timedelta
from pathlib import Path
from threading import RLock

from sourcing_agent.database_config import DatabaseConfig
from sourcing_agent.harness.contracts import JsonObject
from sourcing_agent.sourcing.models import Requirements, TradeRecord

TABLE = "panjivaMXExport"
_DRIVER_LOCK = RLock()

PROJECTION = {
    "record_id": "[panjivaRecordId]",
    "source_table": "N'dbo.panjivaMXExport'",
    "shipment_date": "CONVERT(date,[shpmtDate])",
    "supplier_id": "CASE WHEN [shpPanjivaId]>0 THEN [shpPanjivaId] ELSE NULL END",
    "supplier_name": "LTRIM(RTRIM([shpName]))",
    "supplier_country": "LTRIM(RTRIM([shpCountry]))",
    "buyer_id": "CASE WHEN [conPanjivaId]>0 THEN [conPanjivaId] ELSE NULL END",
    "buyer_name": "LTRIM(RTRIM([conName]))",
    "buyer_country": "LTRIM(RTRIM([conCountry]))",
    "hs_code": "LTRIM(RTRIM([hsCode]))",
    "goods_description": "LTRIM(RTRIM([goodsShipped]))",
    "cif_usd": "[valueOfGoodsItemCIFUSD]",
    "source_file_date": "CONVERT(date,[fileDate])",
}


def like_literal(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def extraction_query(
    requirements: Requirements, max_records: int, scan_limit: int
) -> tuple[str, tuple]:
    if missing := requirements.missing():
        raise ValueError("请提供：" + "、".join(missing))
    if type(max_records) is not int or not 1 <= max_records <= 50000:
        raise ValueError("max_records 必须在 1—50000 之间")
    if type(scan_limit) is not int or not 0 <= scan_limit <= 2000000:
        raise ValueError("scan_limit 必须在 0—2000000 之间；0 表示显式全历史查询")
    if requirements.end_date == date.max:
        raise ValueError("结束日期必须早于 9999-12-31")
    clauses = [
        "[shipment_date]>=%s",
        "[shipment_date]<%s",
        "[supplier_id] IS NOT NULL",
        "[supplier_name] IS NOT NULL",
        "[supplier_name]<>N''",
    ]
    parameters: list[object] = [
        max_records + 1,
        requirements.start_date,
        requirements.end_date + timedelta(days=1),
    ]
    if scan_limit:
        parameters.insert(0, scan_limit)
    for column, value in [
        ("supplier_country", requirements.supplier_country),
        ("buyer_country", requirements.market_country),
    ]:
        if value:
            clauses.append(f"[{column}]=%s")
            parameters.append(value)
    if requirements.hs_prefix:
        clauses.append("PATINDEX(%s,N' '+REPLACE(COALESCE([hs_code],N''),N'.',N''))>0")
        parameters.append("%[^0-9]" + requirements.hs_prefix + "%")
    if requirements.product_text:
        clauses.append("[goods_description] LIKE %s ESCAPE '\\'")
        parameters.append("%" + like_literal(requirements.product_text) + "%")
    if requirements.exclude_supplier_id:
        clauses.append("[supplier_id]<>%s")
        parameters.append(requirements.exclude_supplier_id)
    projection = ",".join(f"{expression} AS [{name}]" for name, expression in PROJECTION.items())
    columns = ",".join(f"[{name}]" for name in PROJECTION)
    # TOP is deliberately inside the CTE, before filters: this table has only a PK index.
    # The bounded primary-key window is explicit in every report, never called a full-market scan.
    window = (
        f"SELECT TOP (%s) {projection} FROM [dbo].[panjivaMXExport] ORDER BY [panjivaRecordId] DESC"
        if scan_limit
        else f"SELECT {projection} FROM [dbo].[panjivaMXExport]"
    )
    query = (
        f"WITH source_window AS ({window}) SELECT TOP (%s) {columns} FROM source_window "
        f"WHERE {' AND '.join(clauses)} ORDER BY [record_id] DESC"
    )
    return query, tuple(parameters)


class SqlServerReader:
    def __init__(self, config: DatabaseConfig) -> None:
        self.config = config

    @contextmanager
    def connection(self) -> Iterator:
        try:
            import pymssql
        except ImportError:
            raise RuntimeError(
                '数据库可选依赖未安装，请运行 pip install -e ".[database]"'
            ) from None
        # FreeTDS timeouts/configuration are process-wide. Serialize this adapter's reads
        # and restore the prior environment after each connection, including exceptions.
        with _DRIVER_LOCK, tempfile.TemporaryDirectory(prefix="sourcing-freetds-") as folder:
            config_path = Path(folder) / "freetds.conf"
            encryption = {"yes": "require", "no": "off", "strict": "strict"}[self.config.encrypt]
            ca = "system" if self.config.trust_server_certificate == "no" else ""
            config_path.write_text(
                f"[global]\nencryption = {encryption}\nca file = {ca}\ncheck certificate hostname = yes\n",
                encoding="utf-8",
            )
            previous = os.environ.get("FREETDSCONF")
            os.environ["FREETDSCONF"] = str(config_path)
            connection = None
            try:
                connection = pymssql.connect(
                    server=self.config.host,
                    port=str(self.config.port),
                    database=self.config.database,
                    user=self.config.user.get_secret_value(),
                    password=self.config.password.get_secret_value(),
                    login_timeout=self.config.login_timeout,
                    timeout=self.config.query_timeout,
                    tds_version="8.0" if self.config.encrypt == "strict" else "7.4",
                    appname="sourcing-agent-readonly",
                    charset="UTF-8",
                    read_only=True,
                    autocommit=False,
                )
                cursor = connection.cursor()
                cursor.execute(f"SET LOCK_TIMEOUT {self.config.query_timeout * 1000}")
                cursor.execute("SELECT DB_NAME()")
                if cursor.fetchone()[0].casefold() != self.config.database.casefold():
                    raise RuntimeError("数据库身份与配置不符")
                cursor.close()
                yield connection
            except pymssql.Error:
                raise RuntimeError(
                    "数据库连接或只读查询失败，请检查本机配置、网络、查询超时和 SELECT 权限"
                ) from None
            finally:
                if connection is not None:
                    with suppress(Exception):
                        connection.rollback()
                    with suppress(Exception):
                        connection.close()
                if previous is None:
                    os.environ.pop("FREETDSCONF", None)
                else:
                    os.environ["FREETDSCONF"] = previous

    def describe(self) -> JsonObject:
        with self.connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT TOP (100) s.name AS schema_name,t.name AS table_name "
                "FROM sys.tables t JOIN sys.schemas s ON t.schema_id=s.schema_id "
                "WHERE t.name LIKE %s ORDER BY s.name,t.name",
                ("%panjiva%",),
            )
            tables = [{"schema": row[0], "table": row[1]} for row in cursor.fetchall()]
            cursor.execute(
                "SELECT TOP (200) c.name,ty.name,c.is_nullable,c.max_length "
                "FROM sys.columns c JOIN sys.tables t ON c.object_id=t.object_id "
                "JOIN sys.schemas s ON t.schema_id=s.schema_id "
                "JOIN sys.types ty ON c.user_type_id=ty.user_type_id "
                "WHERE s.name=%s AND t.name=%s ORDER BY c.column_id",
                ("dbo", TABLE),
            )
            columns = [
                {"name": row[0], "type": row[1], "nullable": bool(row[2]), "max_length": row[3]}
                for row in cursor.fetchall()
            ]
            cursor.close()
        return {"ok": True, "database": self.config.database, "tables": tables, "columns": columns}

    def extract(
        self, requirements: Requirements, max_records: int = 5000, scan_limit: int = 200000
    ) -> tuple[list[TradeRecord], bool]:
        query, parameters = extraction_query(requirements, max_records, scan_limit)
        with self.connection() as connection:
            cursor = connection.cursor()
            cursor.execute(query, parameters)
            rows = cursor.fetchmany(max_records + 1)
            cursor.close()
        truncated = len(rows) > max_records
        try:
            records = [
                TradeRecord.model_validate(dict(zip(PROJECTION, row, strict=True)))
                for row in rows[:max_records]
            ]
        except ValueError:
            # Validation errors echo row contents; do not put records into CLI error diagnostics.
            raise ValueError(
                "源库交易字段不符合当前数据模型；未发布不完整快照，请检查字段映射"
            ) from None
        return records, truncated
