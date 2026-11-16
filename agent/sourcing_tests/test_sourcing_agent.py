"""应用级验收；离线、无密钥，不导入或执行教程的全局运行时。"""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path

from sourcing_agent.adapters.demo import DemoModel
from sourcing_agent.adapters.snapshot import demo_records, publish_snapshot
from sourcing_agent.adapters.trade import TradeRepository
from sourcing_agent.bootstrap import create_runner
from sourcing_agent.harness.context import (
    ContextBudgetExceeded,
    ContextManager,
    ContextPolicy,
    estimate_tokens,
    validate_tool_pairs,
)
from sourcing_agent.harness.contracts import (
    Cancellation,
    Message,
    ModelReply,
    ModelRequest,
    RunCanceled,
    RunLimits,
    ToolCall,
    ToolInputError,
)
from sourcing_agent.harness.memory import MemoryService
from sourcing_agent.harness.store import RuntimeStore
from sourcing_agent.sourcing.models import Requirements
from sourcing_agent.sourcing.research import ResearchTask

QUESTION = "查找 HS 841891，2025 年全年有历史发货的供应商"
SERIAL = 0


def actions(*items: tuple[str, dict]) -> ModelReply:
    global SERIAL
    SERIAL += 1
    return ModelReply(
        finish="tool_calls",
        calls=[
            ToolCall(id=f"call-{SERIAL}-{i}", name=name, arguments=args)
            for i, (name, args) in enumerate(items)
        ],
    )


def discover() -> list[ModelReply]:
    return [actions(("find_supplier_candidates", {})), actions(("submit_findings", {}))]


class ScriptedModel:
    def __init__(self, *responses: ModelReply | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[ModelRequest] = []

    def generate(self, request: ModelRequest) -> ModelReply:
        self.requests.append(request.model_copy(deep=True))
        if request.purpose == "summary":
            return ModelReply(
                text="旧交互已归档。保留 HS、日期、企业与证据，以当前结构化状态为准。"
            )
        if not self.responses:
            raise AssertionError("unexpected model call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class AppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.snapshot = self.root / "trades.sqlite"
        publish_snapshot(self.snapshot, demo_records(), "SYNTHETIC test fixture")
        self.repo = TradeRepository(self.snapshot)

    def runner(self, model, **kwargs):
        return create_runner(self.snapshot, self.root / "state", "project-a", model, **kwargs)

    def test_demo_discovers_compares_and_submits_evidence(self) -> None:
        runner = self.runner(DemoModel())
        session = runner.create_session()
        result = runner.run(session.id, QUESTION)
        self.assertEqual(result.status, "completed")
        self.assertEqual([c["supplier_id"] for c in result.task_state["candidates"]], [20, 10])
        self.assertEqual(result.task_state["comparisons"][0]["winner_ids"], [10])
        self.assertIn("不是票数", result.text)
        self.assertIn("SYNTHETIC", result.text)
        self.assertIn(self.repo.sha256, result.text)
        validate_tool_pairs(result.messages)
        restored = runner.store.load_session(session.id, "project-a")
        self.assertEqual(restored.state, result.task_state)
        self.assertEqual(restored.status, "completed")

    def test_user_revision_invalidates_old_candidates_profiles_and_evidence(self) -> None:
        task = ResearchTask(self.repo, {}, QUESTION)
        task.execute("find_supplier_candidates", {})
        task.execute("investigate_supplier", {"supplier_id": 10})
        task.execute("compare_candidates", {"supplier_ids": [10, 20]})
        task.execute("set_requirements", {"supplier_country": "China"})
        self.assertFalse(task.state.queried)
        self.assertEqual(task.state.evidence, [])
        self.assertEqual(task.state.comparisons, [])
        self.assertEqual(task.state.profiles, [])
        with self.assertRaises(ToolInputError):
            task.execute("query_trade_evidence", {"supplier_id": 10})
        with self.assertRaises(ToolInputError):
            task.execute("submit_findings", {})
        task.execute("find_supplier_candidates", {})
        self.assertEqual([c.supplier_id for c in task.state.candidates], [20])

    def test_explicit_new_hs_clears_old_product_constraint(self) -> None:
        task = ResearchTask(self.repo, {}, QUESTION)
        task.execute("set_requirements", {"product_text": "REFRIGERACION"})
        task.execute("find_supplier_candidates", {})
        next_task = ResearchTask(self.repo, task.checkpoint(), "改查 HS 853910")
        self.assertIsNone(next_task.state.requirements.product_text)
        self.assertFalse(next_task.state.queried)
        with self.assertRaises(ToolInputError):
            next_task.execute("set_requirements", {"hs_prefix": "841891"})

    def test_multiturn_resume_conditions_and_current_candidates(self) -> None:
        model = ScriptedModel(
            actions(("ask_user", {"question": "请提供日期"})),
            *discover(),
        )
        runner = self.runner(model)
        session = runner.create_session()
        self.assertEqual(runner.run(session.id, "查 HS 841891").status, "waiting_user")
        result = runner.run(session.id, "2025 年全年")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.task_state["requirements"]["hs_prefix"], "841891")
        self.assertEqual(len(result.task_state["candidates"]), 2)

    def test_revised_scope_is_visible_and_old_text_does_not_enter_report(self) -> None:
        model = ScriptedModel(*discover())
        runner = self.runner(model)
        session = runner.create_session()
        runner.run(session.id, QUESTION)
        model.responses.extend(
            [
                actions(("set_requirements", {"supplier_country": "China"})),
                *discover(),
            ]
        )
        result = runner.run(session.id, "改成只看中国供应商")
        self.assertNotIn("Demo Supplier A", result.text)
        self.assertIn("Demo Supplier B", result.text)
        self.assertEqual(result.task_state["requirements"]["supplier_country"], "China")

    def test_unknown_tool_feedback_is_paired_and_repairable(self) -> None:
        model = ScriptedModel(actions(("missing_tool", {})), *discover())
        runner = self.runner(model)
        session = runner.create_session()
        result = runner.run(session.id, QUESTION)
        self.assertEqual(result.status, "completed")
        validate_tool_pairs(result.messages)
        self.assertTrue(any(m.role == "tool" and "未知工具" in m.content for m in result.messages))

    def test_partial_tool_output_is_rejected_without_orphan(self) -> None:
        broken = actions(("find_supplier_candidates", {}))
        broken.finish = "length"
        model = ScriptedModel(broken, *discover())
        runner = self.runner(model)
        result = runner.run(runner.create_session().id, QUESTION)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.tool_calls, 2)
        validate_tool_pairs(result.messages)

    def test_duplicate_tool_ids_are_rejected_before_execution(self) -> None:
        broken = actions(("find_supplier_candidates", {}), ("submit_findings", {}))
        broken.calls[1].id = broken.calls[0].id
        model = ScriptedModel(broken, *discover())
        runner = self.runner(model)
        result = runner.run(runner.create_session().id, QUESTION)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.tool_calls, 2)

    def test_pause_waits_for_entire_tool_batch_and_blocks_later_actions(self) -> None:
        model = ScriptedModel(
            actions(
                ("ask_user", {"question": "请确认商品"}),
                ("find_supplier_candidates", {}),
            )
        )
        runner = self.runner(model)
        result = runner.run(runner.create_session().id, QUESTION)
        self.assertEqual(result.status, "waiting_user")
        self.assertFalse(result.task_state["queried"])
        self.assertEqual(sum(m.role == "tool" for m in result.messages), 2)
        validate_tool_pairs(result.messages)

    def test_free_text_claims_cannot_add_candidates_or_metrics(self) -> None:
        model = ScriptedModel(ModelReply(text="虚构企业 Z 有 99999 条记录并已认证"), *discover())
        runner = self.runner(model)
        result = runner.run(runner.create_session().id, QUESTION)
        self.assertEqual(result.status, "completed")
        self.assertNotIn("虚构企业", result.text)
        self.assertNotIn("99999", result.text)

    def test_zero_hit_query_is_valid_but_does_not_claim_market_absence(self) -> None:
        model = ScriptedModel(
            actions(("set_requirements", {"market_country": "Costa Rica"})),
            *discover(),
        )
        runner = self.runner(model)
        result = runner.run(runner.create_session().id, QUESTION)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.task_state["candidates"], [])
        self.assertIn("无候选", result.text)
        self.assertIn("不能据此认定", result.text)

    def test_outside_date_range_text_states_no_overlap(self) -> None:
        task = ResearchTask(self.repo, {}, "找 HS 841891，2026 年全年")
        task.execute("find_supplier_candidates", {})
        self.assertIn("无交集", task.render())

    def test_call_budget_does_not_execute_remaining_actions(self) -> None:
        model = ScriptedModel(
            actions(
                ("find_supplier_candidates", {}),
                ("submit_findings", {}),
            )
        )
        runner = self.runner(model, limits=RunLimits(model_calls=3, tool_calls=1))
        result = runner.run(runner.create_session().id, QUESTION)
        self.assertEqual(result.status, "budget_exhausted")
        self.assertEqual(result.tool_calls, 1)
        validate_tool_pairs(result.messages)

    def test_model_budget_bounds_free_text_loop(self) -> None:
        model = ScriptedModel(ModelReply(text="还在调查"), ModelReply(text="继续"))
        runner = self.runner(model, limits=RunLimits(model_calls=2))
        result = runner.run(runner.create_session().id, QUESTION)
        self.assertEqual(result.status, "budget_exhausted")
        self.assertEqual(result.model_calls, 2)

    def test_failed_turn_preserves_checkpoint_and_explicit_resume_replays_input(self) -> None:
        model = ScriptedModel(*discover())
        runner = self.runner(model)
        session = runner.create_session()
        original = runner.run(session.id, QUESTION)
        model.responses.extend(
            [
                actions(("set_requirements", {"supplier_country": "China"})),
                RuntimeError("provider unavailable"),
            ]
        )
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            runner.run(session.id, "只看中国")
        failed = runner.store.load_session(session.id, "project-a")
        self.assertEqual(failed.state, original.task_state)
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.pending_input, "只看中国")
        with self.assertRaisesRegex(ValueError, "resume"):
            runner.run(session.id, "另一个问题")
        model.responses.extend(
            [
                actions(("set_requirements", {"supplier_country": "China"})),
                *discover(),
            ]
        )
        result = runner.run(session.id, resume=True)
        self.assertEqual(result.status, "completed")
        self.assertEqual([c["supplier_id"] for c in result.task_state["candidates"]], [20])

    def test_cancel_does_not_commit_task_state(self) -> None:
        runner = self.runner(DemoModel())
        session = runner.create_session()
        token = Cancellation()
        token.cancel()
        with self.assertRaises(RunCanceled):
            runner.run(session.id, QUESTION, cancellation=token)
        saved = runner.store.load_session(session.id, "project-a")
        self.assertEqual(saved.status, "canceled")
        self.assertEqual(saved.state, {})
        self.assertEqual(saved.messages, [])
        self.assertEqual(runner.run(session.id, resume=True).status, "completed")

    def test_cross_namespace_and_snapshot_identity_are_enforced(self) -> None:
        runner = self.runner(DemoModel())
        session = runner.create_session()
        with self.assertRaisesRegex(ValueError, "namespace"):
            runner.store.load_session(session.id, "project-b")
        other = self.root / "other.sqlite"
        publish_snapshot(other, demo_records()[:1], "different")
        other_runner = create_runner(other, self.root / "state", "project-a", DemoModel())
        with self.assertRaisesRegex(ValueError, "数据版本"):
            other_runner.run(session.id, QUESTION)

    def test_snapshot_mutation_is_rejected(self) -> None:
        runner = self.runner(DemoModel())
        session = runner.create_session()
        with closing(sqlite3.connect(self.snapshot)) as db, db:
            db.execute("UPDATE metadata SET value='changed' WHERE key='scope'")
        with self.assertRaisesRegex(ValueError, "快照已变化"):
            runner.run(session.id, QUESTION)

    def test_concurrent_start_is_rejected_and_recovery_invalidates_old_owner(self) -> None:
        runner = self.runner(DemoModel())
        session = runner.create_session()
        run_id = runner.store.begin_run(session, QUESTION)
        with self.assertRaises(ValueError):
            runner.store.begin_run(session, QUESTION)
        runner.store.recover_interrupted(session.id, "project-a")
        with self.assertRaisesRegex(ValueError, "所有权"):
            runner.store.fail_run(session, run_id, "failed", "stale owner")
        self.assertEqual(runner.run(session.id, resume=True).status, "completed")

    def test_typed_tool_arguments_reject_boolean_ids_and_unknown_fields(self) -> None:
        task = ResearchTask(self.repo, {}, QUESTION)
        task.execute("find_supplier_candidates", {})
        for arguments in ({"supplier_id": True}, {"supplier_id": 10, "sql": "DROP TABLE trades"}):
            with self.assertRaises(ToolInputError):
                task.execute("query_trade_evidence", arguments)

    def test_bare_year_resolves_to_full_year_period(self) -> None:
        task = ResearchTask(self.repo, {}, "找HS841891在2025年的供应商，排除企业ID20。")
        self.assertEqual(task.state.requirements.hs_prefix, "841891")
        self.assertEqual(task.state.requirements.start_date, date(2025, 1, 1))
        self.assertEqual(task.state.requirements.end_date, date(2025, 12, 31))

    def test_two_years_are_not_guessed(self) -> None:
        task = ResearchTask(self.repo, {}, "找2025年和2026年的供应商")
        self.assertIsNone(task.state.requirements.start_date)
        self.assertIsNone(task.state.requirements.end_date)

    def test_hard_filters_literal_search_and_statistics_match_old_contract(self) -> None:
        req = Requirements(hs_prefix="841891", start_date="2025-01-01", end_date="2025-12-31")
        candidates = self.repo.find_candidates(req)
        self.assertEqual([c.supplier_id for c in candidates], [20, 10])
        self.assertEqual(candidates[0].buyer_count, 0)
        self.assertEqual(candidates[1].record_count, 2)
        self.assertEqual(self.repo.search_products("%"), [])
        self.assertEqual(
            self.repo.find_candidates(req.model_copy(update={"product_text": "%' OR 1=1 --"})), []
        )
        scoped = req.model_copy(update={"supplier_country": "china"})
        self.assertEqual(self.repo.evidence(scoped, 10), [])
        self.assertEqual({e.source.record_id for e in self.repo.evidence(req, 10)}, {1, 2})

    def test_snapshot_publish_does_not_overwrite_or_leave_partial_file(self) -> None:
        original = self.snapshot.read_bytes()
        with self.assertRaises(FileExistsError):
            publish_snapshot(self.snapshot, demo_records(), "overwrite")
        self.assertEqual(self.snapshot.read_bytes(), original)
        duplicate = self.root / "duplicate.sqlite"
        with self.assertRaises(sqlite3.IntegrityError):
            publish_snapshot(duplicate, [demo_records()[0], demo_records()[0]], "duplicate")
        self.assertFalse(duplicate.exists())

    def test_explicit_preferences_are_versioned_scoped_and_removable(self) -> None:
        runner = self.runner(DemoModel())
        store = runner.store
        store.save_memory("project-a", "supplier_country", "China", "user-explicit-1")
        store.save_memory("project-a", "supplier_country", "United States", "user-explicit-2")
        recalled = MemoryService(store, "project-a").recall("supplier_country")
        self.assertEqual(len(recalled), 1)
        self.assertEqual(recalled[0].version, 2)
        self.assertEqual(recalled[0].value, "United States")
        self.assertEqual(store.memories("project-b"), [])
        store.save_memory("project-a", "supplier_country", "", "delete", deleted=True)
        self.assertEqual(store.memories("project-a"), [])

    def test_memory_is_reference_data_and_does_not_silently_change_filters(self) -> None:
        model = ScriptedModel(*discover())
        runner = self.runner(model)
        runner.store.save_memory("project-a", "供应商偏好", "China", "explicit")
        session = runner.create_session()
        result = runner.run(session.id, QUESTION + "，结合供应商偏好")
        reference = json.loads(model.requests[0].messages[0].content)
        self.assertEqual(reference["recalled_preferences"][0]["value"], "China")
        self.assertIsNone(result.task_state["requirements"]["supplier_country"])
        self.assertNotIn("China", model.requests[0].system)


class ContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = RuntimeStore(Path(self.temporary.name) / "runtime.sqlite")
        self.events = []
        self.manager = ContextManager(
            ContextPolicy(window_tokens=6000, tool_result_chars=1000, preview_chars=100),
            self.store,
            "project-a",
            "session-a",
            lambda kind, payload: self.events.append((kind, payload)),
        )

    def prepare(self, messages, model=None, **kwargs):
        return self.manager.prepare(
            model or ScriptedModel(), "Fixed system", messages, "", {}, [], [], 256, 2, **kwargs
        )

    def test_big_result_is_archived_but_protocol_and_original_survive(self) -> None:
        content = json.dumps({"data": "abc" * 5000})
        call = ToolCall(id="id-1", name="query", arguments={})
        messages = [
            Message(role="user", content="question"),
            Message(role="assistant", tool_calls=[call]),
            Message(role="tool", tool_call_id=call.id, content=content),
        ]
        prepared = self.prepare(messages)
        validate_tool_pairs(prepared.messages)
        archived = json.loads(prepared.messages[-1].content)
        full = self.store.read_artifact("project-a", "session-a", archived["artifact_id"], 0, 8000)
        self.assertEqual(full["content"], content[:8000])
        self.assertEqual(messages[-1].content, content)
        self.assertLessEqual(estimate_tokens(prepared.request) + 256, 6000)
        with self.assertRaises(ValueError):
            self.store.read_artifact("project-b", "session-a", archived["artifact_id"])
        with self.assertRaises(ValueError):
            self.store.read_artifact("project-a", "session-b", archived["artifact_id"])

    def test_long_history_is_summarized_and_latest_question_preserved(self) -> None:
        messages = []
        for i in range(8):
            messages.extend(
                [
                    Message(role="user", content=f"old-{i}" + "x" * 500),
                    Message(role="assistant", content="y" * 500),
                ]
            )
        messages.append(Message(role="user", content="CURRENT QUESTION"))
        model = ScriptedModel()
        prepared = self.prepare(messages, model)
        self.assertGreater(prepared.summary_calls, 0)
        self.assertEqual(prepared.messages[-1].content, "CURRENT QUESTION")
        self.assertTrue(prepared.summary)
        self.assertLessEqual(estimate_tokens(prepared.request) + 256, 6000)
        self.assertEqual(len(messages), 17)
        self.assertTrue(any(kind == "context.compacted" for kind, _ in self.events))

    def test_oversized_current_input_fails_without_silent_truncation(self) -> None:
        messages = [Message(role="user", content="x" * 9000)]
        with self.assertRaises(ContextBudgetExceeded):
            self.prepare(messages)
        self.assertEqual(len(messages[0].content), 9000)

    def test_bad_summary_fails_without_changing_checkpoint(self) -> None:
        class BadSummary:
            def generate(self, request):
                return ModelReply(text="", finish="length")

        messages = [
            Message(role=role, content="x" * 1500)
            for _ in range(3)
            for role in ("user", "assistant")
        ]
        with self.assertRaises(ContextBudgetExceeded):
            self.prepare(messages, BadSummary())
        self.assertEqual(len(messages), 6)

    def test_no_compaction_occurs_while_tool_pair_is_pending(self) -> None:
        messages = [
            Message(role="user", content="question"),
            Message(role="assistant", tool_calls=[ToolCall(id="pending", name="query")]),
        ]
        with self.assertRaisesRegex(ValueError, "尚未全部回填"):
            self.prepare(messages)

    def test_summarization_call_count_is_bounded(self) -> None:
        messages = [
            Message(role=role, content="x" * 1000)
            for _ in range(20)
            for role in ("user", "assistant")
        ]
        with self.assertRaises(ContextBudgetExceeded):
            self.manager.prepare(ScriptedModel(), "system", messages, "", {}, [], [], 256, 1)
        self.assertEqual(sum(k == "context.summary_requested" for k, _ in self.events), 1)

    def test_long_single_turn_reduces_consumed_results_without_losing_pairs(self) -> None:
        self.manager.policy = ContextPolicy(
            window_tokens=10000, tool_result_chars=5000, preview_chars=100
        )
        messages = [Message(role="user", content="CURRENT REQUEST")]
        for index in range(6):
            call = ToolCall(id=f"step-{index}", name="query")
            messages.extend(
                [
                    Message(role="assistant", tool_calls=[call]),
                    Message(role="tool", tool_call_id=call.id, content="x" * 2500),
                ]
            )
        prepared = self.prepare(messages)
        validate_tool_pairs(prepared.messages)
        self.assertEqual(prepared.messages[0].content, "CURRENT REQUEST")
        self.assertEqual(prepared.messages[-1].content, "x" * 2500)
        self.assertLessEqual(estimate_tokens(prepared.request) + 256, 10000)
        self.assertEqual(prepared.summary_calls, 0)

    def test_cancellation_is_checked_during_summary(self) -> None:
        token = Cancellation()
        self.manager.check_cancel = token.check

        class CancelingSummary:
            def generate(self, request):
                token.cancel()
                return ModelReply(text="summary")

        messages = [
            Message(role=role, content="x" * 1200)
            for _ in range(4)
            for role in ("user", "assistant")
        ]
        with self.assertRaises(RunCanceled):
            self.prepare(messages, CancelingSummary())

    def test_read_result_tool_can_retrieve_an_archive_in_agent_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = root / "trades.sqlite"
            publish_snapshot(snapshot, demo_records(), "SYNTHETIC")
            model = ScriptedModel()
            runner = create_runner(snapshot, root, "project-a", model)
            session = runner.create_session()
            artifact = runner.store.archive("project-a", session.id, '{"verified":true}')
            model.responses.extend(
                [actions(("read_result", {"artifact_id": artifact})), *discover()]
            )
            result = runner.run(session.id, QUESTION)
            self.assertEqual(result.status, "completed")
            self.assertTrue(
                any(m.role == "tool" and "verified" in m.content for m in result.messages)
            )


if __name__ == "__main__":
    unittest.main()
