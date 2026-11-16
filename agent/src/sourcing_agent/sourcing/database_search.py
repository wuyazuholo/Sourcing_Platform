"""Freeze a bounded real query before using the existing research/evidence contracts."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sourcing_agent.adapters.snapshot import publish_snapshot
from sourcing_agent.adapters.sqlserver import SqlServerReader
from sourcing_agent.adapters.trade import TradeRepository
from sourcing_agent.bootstrap import create_runner
from sourcing_agent.database_config import DatabaseConfig
from sourcing_agent.harness.contracts import (
    EngineName,
    JsonObject,
    ModelReply,
    ModelRequest,
    RunLimits,
    ToolCall,
)
from sourcing_agent.harness.store import encode
from sourcing_agent.sourcing.models import Requirements


class DatabaseSearchModel:
    """Deterministic query report; does not transmit database records to a model service."""

    def __init__(self, requirements: Requirements) -> None:
        self.requirements = requirements
        self.index = 0

    def generate(self, request: ModelRequest) -> ModelReply:
        if request.purpose == "summary":
            return ModelReply(finish="stop", text="数据库筛选条件和证据以持久化任务状态为准。")
        name, arguments = [
            ("set_requirements", self.requirements.model_dump(mode="json")),
            ("find_supplier_candidates", {}),
            ("submit_findings", {}),
        ][self.index]
        self.index += 1
        return ModelReply(
            finish="tool_calls",
            calls=[ToolCall(id=f"database-{self.index}", name=name, arguments=arguments)],
        )


def search_database(
    config: DatabaseConfig,
    requirements: Requirements,
    state_dir: Path,
    *,
    max_records: int = 5000,
    scan_limit: int = 200000,
    engine: EngineName = "native",
) -> JsonObject:
    if engine not in {"native", "langgraph"}:
        raise ValueError("不支持的执行引擎")
    records, truncated = SqlServerReader(config).extract(requirements, max_records, scan_limit)
    folder = state_dir / uuid4().hex
    snapshot = folder / "trades.sqlite"
    timestamp = datetime.now(UTC).isoformat()
    coverage = (
        f"按原始主键倒序最近至多 {scan_limit} 行内筛选"
        if scan_limit
        else "按条件检索该表全部历史范围（受查询超时及匹配记录保存上限限制）"
    )
    scope = (
        f"SQL Server {config.database}.dbo.panjivaMXExport 墨西哥出口数据实时只读提取；"
        f"{coverage}；条件={encode(requirements.model_dump(mode='json'))}；"
        f"实际保存 {len(records)} 行；提取时间={timestamp}。"
        + (
            "这是有界源库检索窗口，不覆盖整张表，也不代表完整市场。"
            if scan_limit
            else "单一来源数据库仍不代表完整市场。"
        )
    )
    if truncated:
        scope += f" 匹配结果超过 {max_records} 行保存上限，统计仅覆盖保存部分，请缩小条件。"
    publish_snapshot(
        snapshot,
        records,
        scope,
        allow_empty=True,
        extra_metadata={
            "source_kind": "sqlserver",
            "source_database": config.database,
            "source_query": encode(requirements.model_dump(mode="json")),
            "source_extracted_at": timestamp,
            "source_scan_limit": str(scan_limit),
            "source_result_truncated": str(truncated).lower(),
        },
    )
    runner = create_runner(
        snapshot,
        folder,
        "database-search",
        DatabaseSearchModel(requirements),
        limits=RunLimits(model_calls=3),
        engine=engine,
    )
    session = runner.create_session()
    message = f"数据库查询：HS {requirements.hs_prefix or ''}，{requirements.start_date} 至 {requirements.end_date}。按明确条件生成证据报告。"
    result = runner.run(session.id, message)
    repository = TradeRepository(snapshot)
    profiles = [
        repository.profile(requirements, candidate["supplier_id"]).model_dump(mode="json")
        for candidate in result.task_state["candidates"]
    ]
    repository.verify()
    report = {
        "ok": result.status == "completed",
        "source": "live_sqlserver_extract",
        "database": config.database,
        "table": "dbo.panjivaMXExport",
        "engine": engine,
        "extracted_at": timestamp,
        "scan_limit": scan_limit,
        "max_records": max_records,
        "full_history": scan_limit == 0,
        "record_count": len(records),
        "result_truncated": truncated,
        "snapshot": str(snapshot.resolve()),
        "snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        "state_dir": str(folder.resolve()),
        "session_id": session.id,
        "requirements": requirements.model_dump(mode="json"),
        "candidates": result.task_state["candidates"],
        "candidate_profiles": profiles,
        "evidence": result.task_state["evidence"],
        "text": result.text,
        "business_data_sent_to_model": False,
    }
    (folder / "report.json").write_text(encode(report), encoding="utf-8")
    return report
