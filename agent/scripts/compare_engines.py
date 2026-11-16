"""同一快照与受控模型响应对比两种引擎；不调用远端模型。"""

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from sourcing_agent.adapters.snapshot import demo_records, publish_snapshot
from sourcing_agent.bootstrap import create_runner
from sourcing_agent.harness.context import estimate_tokens
from sourcing_agent.harness.contracts import (
    EngineName,
    JsonObject,
    ModelReply,
    ModelRequest,
    RunLimits,
    ToolCall,
)
from sourcing_agent.harness.store import encode


class ReplayModel:
    def __init__(self, replies: list[ModelReply | Exception]) -> None:
        self.replies = list(replies)

    def generate(self, request: ModelRequest) -> ModelReply:
        if request.purpose == "summary":
            return ModelReply(text="先前交互已归档；以当前有效需求与证据为准。")
        if not self.replies:
            raise RuntimeError("对照脚本出现未预期的模型请求")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def call(name: str, arguments: JsonObject | None = None, index: int = 0) -> ModelReply:
    return ModelReply(
        finish="tool_calls",
        calls=[ToolCall(id=f"action-{index}", name=name, arguments=arguments or {})],
    )


@dataclass(frozen=True)
class Scenario:
    name: str
    messages: tuple[str, ...]
    replies: tuple[ModelReply, ...]
    candidate_ids: tuple[int, ...]
    status: str = "completed"
    comparison: bool = False
    profile: bool = False
    max_model_calls: int = 10


QUESTION = "找 HS 841891，2025 年全年的历史供应商"


def scenarios() -> list[Scenario]:
    find, submit = call("find_supplier_candidates"), call("submit_findings", index=3)
    return [
        Scenario("discovery", (QUESTION,), (find, submit), (20, 10)),
        Scenario(
            "comparison",
            (QUESTION + "并比较",),
            (find, call("compare_candidates", {"supplier_ids": [20, 10]}, 1), submit),
            (20, 10),
            comparison=True,
        ),
        Scenario(
            "investigation",
            (QUESTION + "并补查企业10",),
            (find, call("investigate_supplier", {"supplier_id": 10}, 1), submit),
            (20, 10),
            profile=True,
        ),
        Scenario(
            "clarification",
            ("找 HS 841891", "2025 年全年"),
            (call("ask_user", {"question": "请补充日期"}), find, submit),
            (20, 10),
        ),
        Scenario(
            "revision",
            (QUESTION, "只看 China"),
            (
                find,
                submit,
                call("set_requirements", {"supplier_country": "China"}, 4),
                find,
                submit,
            ),
            (20,),
        ),
        Scenario("outside_coverage", ("找 HS 841891，2026 年全年",), (find, submit), ()),
        Scenario("bad_tool", (QUESTION,), (call("unknown_tool"), find, submit), (20, 10)),
        Scenario(
            "truncated_response", (QUESTION,), (ModelReply(finish="length"), find, submit), (20, 10)
        ),
        Scenario(
            "model_budget",
            (QUESTION,),
            (ModelReply(text="调查中"), ModelReply(text="继续调查")),
            (),
            status="budget_exhausted",
            max_model_calls=2,
        ),
    ]


def metrics(events: list[JsonObject]) -> JsonObject:
    requests = [e["payload"] for e in events if e["type"] == "model.request"]
    return {
        "model_requests": len(requests),
        "tool_results": sum(e["type"] == "tool.result" for e in events),
        "estimated_input_tokens": sum(
            estimate_tokens(ModelRequest.model_validate(r)) for r in requests
        ),
        "summary_requests": sum(e["type"] == "context.summary_requested" for e in events),
    }


def execute_case(engine: EngineName, snapshot: Path, directory: Path, case: Scenario) -> JsonObject:
    model = ReplayModel(list(case.replies))
    runner = create_runner(
        snapshot,
        directory / engine,
        "comparison",
        model,
        engine=engine,
        limits=RunLimits(model_calls=case.max_model_calls),
    )
    session = runner.create_session()
    start = perf_counter()
    outcomes = [runner.run(session.id, message) for message in case.messages]
    elapsed = (perf_counter() - start) * 1000
    final = outcomes[-1]
    ids = [c["supplier_id"] for c in final.task_state["candidates"]]
    checks = {
        "candidate_ids": ids == list(case.candidate_ids),
        "status": final.status == case.status,
        "protocol": True,
        "comparison": not case.comparison
        or final.task_state["comparisons"][0]["winner_ids"] == [10],
        "profile": not case.profile or final.task_state["profiles"][0]["supplier_id"] == 10,
    }
    from sourcing_agent.harness.context import validate_tool_pairs

    validate_tool_pairs(final.messages)
    projection = {
        "statuses": [o.status for o in outcomes],
        "task_state": final.task_state,
        "text": final.text,
    }
    return {
        "engine": engine,
        "case": case.name,
        "checks": checks,
        "projection_sha256": hashlib.sha256(encode(projection).encode()).hexdigest(),
        "elapsed_ms": round(elapsed, 3),
        **metrics(runner.store.events(session.id, "comparison")),
    }


def recovery_case(engine: EngineName, snapshot: Path, directory: Path) -> JsonObject:
    runner = create_runner(
        snapshot,
        directory / ("recovery-" + engine),
        "recovery",
        ReplayModel([call("find_supplier_candidates"), RuntimeError("simulated outage")]),
        engine=engine,
    )
    session = runner.create_session()
    try:
        runner.run(session.id, QUESTION)
    except RuntimeError as error:
        if str(error) != "simulated outage":
            raise
    else:
        raise AssertionError("预期的故障没有发生")
    responses = [call("find_supplier_candidates")] if engine == "native" else []
    responses.append(call("submit_findings", index=3))
    restarted = create_runner(
        snapshot,
        directory / ("recovery-" + engine),
        "recovery",
        ReplayModel(responses),
        engine=engine,
    )
    outcome = restarted.run(session.id, resume=True)
    events = restarted.store.events(session.id, "recovery")
    find_results = sum(
        e["type"] == "tool.result" and "candidates" in e["payload"]["result"] for e in events
    )
    return {
        "engine": engine,
        "status": outcome.status,
        "successful_find_executions": find_results,
        **metrics(events),
    }


def compare(output_dir: Path) -> JsonObject:
    directory = output_dir / uuid4().hex
    directory.mkdir(parents=True)
    snapshot = directory / "synthetic.sqlite"
    publish_snapshot(snapshot, demo_records(), "SYNTHETIC engine comparison; six records")
    results = []
    for case in scenarios():
        native = execute_case("native", snapshot, directory, case)
        graph = execute_case("langgraph", snapshot, directory, case)
        results.append(
            {
                "case": case.name,
                "native": native,
                "langgraph": graph,
                "equivalent": native["projection_sha256"] == graph["projection_sha256"],
            }
        )
    recovery = {
        engine: recovery_case(engine, snapshot, directory) for engine in ("native", "langgraph")
    }
    report = {
        "mode": "offline_scripted_contract_comparison",
        "business_data_sent": False,
        "case_count": len(results),
        "equivalent_cases": sum(row["equivalent"] for row in results),
        "all_expected_checks_passed": all(
            all(row[engine]["checks"].values())
            for row in results
            for engine in ("native", "langgraph")
        ),
        "cases": results,
        "recovery": recovery,
        "note": "受控模型响应；耗时只衡量本地编排开销，输入Token为保守估算，不代表真实模型成本或效果。",
    }
    report_file = directory / "report.json"
    report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**{k: v for k, v in report.items() if k != "cases"}, "report": str(report_file)}


def main() -> None:
    parser = argparse.ArgumentParser(description="离线对比原生与 LangGraph 引擎")
    parser.add_argument("--output-dir", type=Path, default=Path(".runtime/engine-comparison"))
    args = parser.parse_args()
    report = compare(args.output_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if (
        report["equivalent_cases"] != report["case_count"]
        or not report["all_expected_checks_passed"]
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
