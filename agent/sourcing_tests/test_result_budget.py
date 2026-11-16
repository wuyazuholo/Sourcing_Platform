"""Regression coverage for visible archive pages and bounded supplier discovery."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from test_sourcing_agent import actions

from sourcing_agent.adapters.snapshot import publish_snapshot
from sourcing_agent.bootstrap import create_runner
from sourcing_agent.harness.context import ContextPolicy, validate_tool_pairs
from sourcing_agent.harness.contracts import RunLimits
from sourcing_agent.harness.store import RuntimeStore, encode

SPEC = importlib.util.spec_from_file_location(
    "budget_eval", Path(__file__).resolve().parents[1] / "scripts/rsi_sourcing_eval.py"
)
assert SPEC is not None and SPEC.loader is not None
EVAL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVAL)
ENGINES = ["native"] + (["langgraph"] if importlib.util.find_spec("langgraph") else [])


class ResultBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.snapshot = self.root / "trades.sqlite"
        publish_snapshot(self.snapshot, EVAL.synthetic_records(), "SYNTHETIC budget regression")

    def test_pages_fit_serialized_limit_and_reconstruct_original(self):
        store = RuntimeStore(self.root / "state.sqlite")
        content = ('中文 "\\\n\t' * 900) + "END"
        artifact = store.archive("project", "session", content)
        offset = 0
        pages = []
        while True:
            page = store.read_artifact(
                "project", "session", artifact, offset, 8000, max_result_chars=1000
            )
            self.assertLessEqual(len(encode(page)), 1000)
            self.assertEqual(page["offset"], offset)
            pages.append(page["content"])
            if not page["has_more"]:
                self.assertIsNone(page["next_offset"])
                break
            self.assertEqual(page["next_offset"], offset + len(page["content"]))
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        self.assertEqual("".join(pages), content)
        with self.assertRaises(ValueError):
            store.read_artifact("other", "session", artifact, max_result_chars=1000)

    def test_recent_read_pages_remain_visible_in_both_engines(self):
        for engine in ENGINES:
            with self.subTest(engine=engine):
                expected = '中文 "\\\n' * 220

                class PageReader:
                    artifact = ""
                    recovered = ""
                    calls = 0

                    def generate(model, request):
                        model.calls += 1
                        results = [m for m in request.messages if m.role == "tool"]
                        if not results:
                            return actions(
                                ("read_result", {"artifact_id": model.artifact, "limit": 8000})
                            )
                        result = json.loads(results[-1].content)
                        if "content" in result:
                            self.assertNotIn("truncated", result)
                            self.assertLessEqual(len(results[-1].content), 1200)
                            model.recovered += result["content"]
                            if result["has_more"]:
                                return actions(
                                    (
                                        "read_result",
                                        {
                                            "artifact_id": model.artifact,
                                            "offset": result["next_offset"],
                                            "limit": 8000,
                                        },
                                    )
                                )
                            return actions(
                                ("find_supplier_candidates", {}), ("submit_findings", {})
                            )
                        self.assertIn("candidates", result)
                        return actions(("submit_findings", {}))

                model = PageReader()
                runner = create_runner(
                    self.snapshot,
                    self.root / engine,
                    "project",
                    model,
                    policy=ContextPolicy(window_tokens=60000, tool_result_chars=1200),
                    limits=RunLimits(model_calls=7),
                    engine=engine,
                )
                session = runner.create_session()
                model.artifact = runner.store.archive("project", session.id, expected)
                result = runner.run(session.id, "找 HS 841891，2025年全年")
                self.assertEqual(result.status, "completed")
                self.assertEqual(model.recovered, expected)
                self.assertLessEqual(result.model_calls, 7)
                validate_tool_pairs(result.messages)

    def test_discovery_and_date_reply_complete_without_archive_reads(self):
        for engine in ENGINES:
            for clarify in [False, True]:
                with self.subTest(engine=engine, clarify=clarify):

                    class DiscoveryModel:
                        date_reply = clarify
                        results = []

                        def generate(model, request):
                            state = json.loads(request.messages[0].content)["effective_task_state"]
                            if state["missing"]:
                                return actions(("ask_user", {"question": "请提供起止日期"}))
                            if (
                                not model.date_reply
                                and state["requirements"]["market_country"] != "Colombia"
                            ):
                                return actions(("set_requirements", {"market_country": "Colombia"}))
                            if not state["queried"]:
                                return actions(("find_supplier_candidates", {}))
                            result = json.loads(
                                [m for m in request.messages if m.role == "tool"][-1].content
                            )
                            self.assertNotIn("truncated", result)
                            self.assertLessEqual(len(encode(result)), 4000)
                            self.assertTrue(result["candidates"])
                            for candidate in result["candidates"]:
                                self.assertIn("supplier_name", candidate)
                                self.assertGreater(candidate["evidence_count"], 0)
                                self.assertTrue(candidate["evidence_id"].startswith("trade:"))
                            model.results.append(result)
                            return actions(("submit_findings", {}))

                    model = DiscoveryModel()
                    runner = create_runner(
                        self.snapshot,
                        self.root / f"{engine}-{clarify}",
                        "project",
                        model,
                        limits=RunLimits(model_calls=7),
                        engine=engine,
                    )
                    session = runner.create_session()
                    if clarify:
                        first = runner.run(session.id, "找HS841891的供应商。")
                        self.assertEqual(first.status, "waiting_user")
                        message = "2025年全年。"
                    else:
                        message = "找HS841891在2025年全年的供应商，目标市场哥伦比亚。"
                    result = runner.run(session.id, message)
                    self.assertEqual(result.status, "completed")
                    self.assertLessEqual(result.model_calls, 3)
                    events = runner.store.events(session.id, "project")
                    self.assertFalse(
                        any(
                            e["type"] == "tool.started" and e["payload"]["name"] == "read_result"
                            for e in events
                        )
                    )
                    self.assertIn("交易证据", result.text)
                    self.assertTrue(
                        all(
                            e["source"]["snapshot_sha256"] == result.task_state["snapshot_sha256"]
                            for e in result.task_state["evidence"]
                        )
                    )
                    validate_tool_pairs(result.messages)


if __name__ == "__main__":
    unittest.main()
