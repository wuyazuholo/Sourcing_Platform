"""本地数据检查与可选的无业务数据模型连通性测试。"""

import os
from pathlib import Path

from sourcing_agent.adapters.models import configured_model
from sourcing_agent.adapters.trade import TradeRepository
from sourcing_agent.harness.contracts import Message, ModelRequest, ToolDefinition


def diagnose(snapshot: Path, provider: str, live: bool = False) -> dict[str, object]:
    repository = TradeRepository(snapshot)
    with repository.connect() as db:
        integrity = db.execute("PRAGMA quick_check").fetchone()[0]
        row_count = db.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    report: dict[str, object] = {
        "snapshot": {
            "path": str(snapshot.resolve()),
            "sha256": repository.sha256,
            "row_count": row_count,
            "date_range": [repository.start_date, repository.end_date],
            "scope": repository.metadata.get("scope"),
            "integrity": integrity,
        },
        "provider": provider,
        "model": os.getenv("MODEL_ID")
        if provider == "anthropic"
        else os.getenv("SOURCING_MODEL_NAME"),
        "live_requested": live,
    }
    if integrity != "ok":
        raise ValueError("交易快照完整性检查失败")
    if not live:
        return report
    model = configured_model(provider)
    # 两次最小请求只含固定测试内容，不发送仓库里的供应商或交易记录。
    request = ModelRequest(
        system="Connectivity test. Call connectivity_probe exactly once with {}. Do not add arguments.",
        messages=[Message(role="user", content="Run the connectivity probe.")],
        tools=[
            ToolDefinition(
                name="connectivity_probe",
                description="A harmless local connectivity probe.",
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
            )
        ],
        max_output_tokens=256,
    )
    reply = model.generate(request)
    valid = (
        reply.finish == "tool_calls"
        and len(reply.calls) == 1
        and reply.calls[0].name == "connectivity_probe"
        and reply.calls[0].arguments == {}
    )
    report["tool_call_verified"] = valid
    if not valid:
        raise ValueError("模型已响应，但未生成预期的连通性测试工具调用")
    request.messages.extend(
        [
            Message(
                role="assistant",
                content=reply.text,
                tool_calls=reply.calls,
                provider_data=reply.provider_data,
            ),
            Message(role="tool", tool_call_id=reply.calls[0].id, content='{"ok":true}'),
        ]
    )
    request.system = "Connectivity test. The probe succeeded. Reply with OK. Do not call tools."
    request.tools = []
    acknowledgement = model.generate(request)
    report["round_trip_verified"] = (
        acknowledgement.finish == "stop"
        and not acknowledgement.calls
        and bool(acknowledgement.text.strip())
    )
    if not report["round_trip_verified"]:
        raise ValueError("模型工具结果回填后的确认响应不完整")
    report["business_data_sent"] = False
    return report
