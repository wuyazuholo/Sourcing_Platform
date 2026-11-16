"""相同业务合约跑两种引擎，并单独验证图检查点与中断恢复。"""

import importlib.util
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import test_live_sdk_contracts as sdk_fixtures
import test_sourcing_agent as fixtures

from sourcing_agent.adapters.demo import DemoModel
from sourcing_agent.adapters.trade import TradeRepository
from sourcing_agent.bootstrap import create_runner
from sourcing_agent.harness.contracts import RunLimits
from sourcing_agent.harness.store import RuntimeStore

HAS_GRAPH = importlib.util.find_spec("langgraph") is not None


@unittest.skipUnless(HAS_GRAPH, "optional graph dependencies not installed")
class GraphAppTests(fixtures.AppTests):
    def runner(self, model, **kwargs):
        return create_runner(
            self.snapshot, self.root / "state", "project-a", model, engine="langgraph", **kwargs
        )

    def test_graph_does_not_call_native_loop(self) -> None:
        runner = self.runner(DemoModel())
        session = runner.create_session()
        with patch(
            "sourcing_agent.harness.loop.AgentLoop.run", side_effect=AssertionError("native")
        ):
            result = runner.run(session.id, fixtures.QUESTION)
        self.assertEqual(result.status, "completed")
        self.assertTrue((self.root / "state/langgraph-checkpoints.sqlite").is_file())
        events = runner.store.events(session.id, "project-a")
        nodes = {e["payload"]["node"] for e in events if e["type"] == "graph.node.started"}
        self.assertTrue(
            {"prepare_context", "call_model", "execute_tool", "assess", "finalize"}.issubset(nodes)
        )

    def test_restart_resumes_after_completed_tool_nodes(self) -> None:
        model = fixtures.ScriptedModel(
            fixtures.actions(
                ("find_supplier_candidates", {}), ("investigate_supplier", {"supplier_id": 10})
            ),
            RuntimeError("temporary provider failure"),
        )
        runner = self.runner(model)
        session = runner.create_session()
        original = TradeRepository.find_candidates
        calls = []

        def counted(repository, requirements):
            calls.append(requirements)
            return original(repository, requirements)

        with patch.object(TradeRepository, "find_candidates", counted):
            with self.assertRaisesRegex(RuntimeError, "provider failure"):
                runner.run(session.id, fixtures.QUESTION)
            self.assertEqual(runner.store.load_session(session.id, "project-a").state, {})
            restarted_model = fixtures.ScriptedModel(fixtures.actions(("submit_findings", {})))
            restarted = self.runner(restarted_model)
            result = restarted.run(session.id, resume=True)
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(restarted_model.requests), 1)
        self.assertTrue(result.task_state["profiles"])

    def test_retry_of_second_tool_does_not_repeat_first_tool(self) -> None:
        runner = self.runner(
            fixtures.ScriptedModel(
                fixtures.actions(
                    ("find_supplier_candidates", {}), ("investigate_supplier", {"supplier_id": 10})
                )
            )
        )
        session = runner.create_session()
        with patch.object(TradeRepository, "profile", side_effect=RuntimeError("profile offline")):
            with self.assertRaisesRegex(RuntimeError, "profile offline"):
                runner.run(session.id, fixtures.QUESTION)
        restarted = self.runner(fixtures.ScriptedModel(fixtures.actions(("submit_findings", {}))))
        result = restarted.run(session.id, resume=True)
        self.assertEqual(result.status, "completed")
        events = restarted.store.events(session.id, "project-a")
        successful_find = [
            e
            for e in events
            if e["type"] == "tool.result" and "candidates" in e["payload"]["result"]
        ]
        self.assertEqual(len(successful_find), 1)
        self.assertEqual(result.tool_calls, 4)

    def test_interrupt_survives_runner_restart(self) -> None:
        runner = self.runner(
            fixtures.ScriptedModel(fixtures.actions(("ask_user", {"question": "请提供日期"})))
        )
        session = runner.create_session()
        first = runner.run(session.id, "找 HS 841891")
        self.assertEqual(first.status, "waiting_user")
        restarted = self.runner(fixtures.ScriptedModel(*fixtures.discover()))
        result = restarted.run(session.id, "2025 年全年")
        self.assertEqual(result.status, "completed")
        self.assertEqual(
            sum(m.role == "user" and m.content == "2025 年全年" for m in result.messages), 1
        )
        events = restarted.store.events(session.id, "project-a")
        self.assertTrue(any(e["type"] == "graph.user_resumed" for e in events))

    def test_finished_graph_can_commit_after_application_commit_failure(self) -> None:
        runner = self.runner(fixtures.ScriptedModel(*fixtures.discover()))
        session = runner.create_session()
        with patch.object(
            runner.store, "commit_run", side_effect=RuntimeError("commit unavailable")
        ):
            with self.assertRaisesRegex(RuntimeError, "commit unavailable"):
                runner.run(session.id, fixtures.QUESTION)
        empty_model = fixtures.ScriptedModel()
        restarted = self.runner(empty_model)
        result = restarted.run(session.id, resume=True)
        self.assertEqual(result.status, "completed")
        self.assertEqual(empty_model.requests, [])

    def test_interrupt_commit_failure_does_not_treat_original_input_as_reply(self) -> None:
        runner = self.runner(
            fixtures.ScriptedModel(fixtures.actions(("ask_user", {"question": "请提供日期"})))
        )
        session = runner.create_session()
        with patch.object(
            runner.store, "commit_run", side_effect=RuntimeError("commit unavailable")
        ):
            with self.assertRaises(RuntimeError):
                runner.run(session.id, "找 HS 841891")
        restarted = self.runner(fixtures.ScriptedModel())
        result = restarted.run(session.id, resume=True)
        self.assertEqual(result.status, "waiting_user")
        restarted.model.responses.extend(fixtures.discover())
        self.assertEqual(restarted.run(session.id, "2025 年全年").status, "completed")

    def test_new_turn_with_identical_text_is_not_confused_with_previous_checkpoint(self) -> None:
        runner = self.runner(fixtures.ScriptedModel(*fixtures.discover()))
        session = runner.create_session()
        runner.run(session.id, fixtures.QUESTION)
        with patch(
            "sourcing_agent.harness.graph.LangGraphLoop.run",
            side_effect=RuntimeError("before graph"),
        ):
            with self.assertRaises(RuntimeError):
                runner.run(session.id, fixtures.QUESTION)
        restarted_model = fixtures.ScriptedModel(fixtures.actions(("submit_findings", {})))
        result = self.runner(restarted_model).run(session.id, resume=True)
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(restarted_model.requests), 1)

    def test_engine_cannot_be_changed_with_existing_session(self) -> None:
        native = create_runner(self.snapshot, self.root / "state", "project-a", DemoModel())
        session = native.create_session()
        graph = self.runner(fixtures.ScriptedModel())
        with self.assertRaisesRegex(ValueError, "相同 --engine"):
            graph.run(session.id, fixtures.QUESTION)
        self.assertEqual(graph.model.requests, [])

    def test_changed_limits_cannot_silently_resume_graph(self) -> None:
        runner = self.runner(fixtures.ScriptedModel(RuntimeError("offline")))
        session = runner.create_session()
        with self.assertRaises(RuntimeError):
            runner.run(session.id, fixtures.QUESTION)
        restarted = self.runner(DemoModel(), limits=RunLimits(model_calls=2))
        with self.assertRaisesRegex(ValueError, "预算必须"):
            restarted.run(session.id, resume=True)

    def test_completed_next_turn_uses_fresh_model_budget(self) -> None:
        runner = self.runner(
            fixtures.ScriptedModel(*fixtures.discover(), fixtures.actions(("submit_findings", {})))
        )
        session = runner.create_session()
        first = runner.run(session.id, fixtures.QUESTION)
        second = runner.run(session.id, "继续看当前结果")
        self.assertEqual(first.model_calls, 2)
        self.assertEqual(second.model_calls, 1)

    def test_offline_comparison_matches_all_expected_business_results(self) -> None:
        import runpy

        script = Path(__file__).resolve().parents[1] / "scripts/compare_engines.py"
        compare = runpy.run_path(str(script))["compare"]
        report = compare(self.root / "comparison")
        self.assertEqual(report["case_count"], 9)
        self.assertEqual(report["equivalent_cases"], 9)
        self.assertTrue(report["all_expected_checks_passed"])
        self.assertFalse(report["business_data_sent"])
        self.assertEqual(report["recovery"]["native"]["successful_find_executions"], 2)
        self.assertEqual(report["recovery"]["langgraph"]["successful_find_executions"], 1)

    def test_multiple_interrupts_in_one_conversation_do_not_reuse_old_answer(self) -> None:
        model = fixtures.ScriptedModel(
            fixtures.actions(("ask_user", {"question": "请提供日期"})),
            fixtures.actions(("ask_user", {"question": "请确认供应商国家"})),
            fixtures.actions(("set_requirements", {"supplier_country": "China"})),
            *fixtures.discover(),
        )
        runner = self.runner(model)
        session = runner.create_session()
        self.assertEqual(runner.run(session.id, "查 HS 841891").status, "waiting_user")
        self.assertEqual(runner.run(session.id, "2025 年全年").status, "waiting_user")
        result = runner.run(session.id, "只看 China")
        self.assertEqual(result.status, "completed")
        self.assertEqual([c["supplier_id"] for c in result.task_state["candidates"]], [20])


@unittest.skipUnless(HAS_GRAPH, "optional graph dependencies not installed")
class GraphSdkContracts(sdk_fixtures.SdkContracts):
    def setUp(self) -> None:
        super().setUp()
        original = sdk_fixtures.create_runner
        patcher = patch.object(
            sdk_fixtures,
            "create_runner",
            side_effect=lambda *a, **k: original(*a, **k, engine="langgraph"),
        )
        patcher.start()
        self.addCleanup(patcher.stop)


class EngineIsolationTests(unittest.TestCase):
    def test_native_import_does_not_load_langgraph(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from sourcing_agent.bootstrap import create_runner; "
                "assert 'langgraph' not in sys.modules; print('native is independent')",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_legacy_sessions_are_migrated_as_native_without_rewriting_state(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute(
                    "CREATE TABLE sessions(id TEXT PRIMARY KEY,namespace TEXT,"
                    "snapshot_sha256 TEXT,status TEXT,state TEXT,messages TEXT,summary TEXT,"
                    "pending_input TEXT,revision INTEGER)"
                )
                db.execute(
                    "INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        "legacy",
                        "project",
                        "digest",
                        "completed",
                        '{"keep":true}',
                        "[]",
                        "",
                        None,
                        7,
                    ),
                )
            store = RuntimeStore(path)
            session = store.load_session("legacy", "project")
            self.assertEqual(session.engine, "native")
            self.assertEqual(session.state, {"keep": True})
            self.assertEqual(session.revision, 7)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(
                    db.execute("SELECT state FROM sessions").fetchone()[0], '{"keep":true}'
                )


if __name__ == "__main__":
    unittest.main()
