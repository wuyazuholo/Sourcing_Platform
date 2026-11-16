"""RSI 提示词评测脚本的离线验收；不需要模型密钥。"""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sourcing_agent.adapters.snapshot import publish_snapshot
from sourcing_agent.harness.contracts import ModelReply, ModelRequest, ToolCall

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SPEC = importlib.util.spec_from_file_location("rsi_sourcing_eval", SCRIPTS / "rsi_sourcing_eval.py")
assert SPEC is not None and SPEC.loader is not None
rsi = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rsi)

SERIAL = 0


def reply(name: str, arguments: dict | None = None) -> ModelReply:
    global SERIAL
    SERIAL += 1
    return ModelReply(
        finish="tool_calls",
        calls=[ToolCall(id=f"rsi-{SERIAL}", name=name, arguments=arguments or {})],
    )


class ScriptedModel:
    def __init__(self, responses: list[ModelReply]) -> None:
        self.responses = list(responses)

    def generate(self, request: ModelRequest) -> ModelReply:
        if request.purpose == "summary":
            return ModelReply(text="旧交互已归档；以当前有效需求与证据为准。")
        if not self.responses:
            raise AssertionError("评测模型出现未预期的请求")
        return self.responses.pop(0)


def outcome(state: dict, status: str = "completed", text: str = "") -> SimpleNamespace:
    return SimpleNamespace(task_state=state, status=status, text=text)


class FixtureLoadingTests(unittest.TestCase):
    def test_cases_grouped_by_fixture_split(self) -> None:
        grouped = rsi.load_cases(rsi.FIXTURE)
        ids = [case["id"] for cases in grouped.values() for case in cases]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(
            {case["id"] for case in grouped["development"]},
            {
                "discover",
                "clarify-date",
                "revise-clear-country",
                "compare-buyers",
                "compare-recency",
                "buyer-profile",
                "monthly-profile",
                "compare-then-research",
                "rerank",
                "no-product",
                "time-outside",
                "unknown-certification",
            },
        )
        self.assertEqual(
            {case["id"] for case in grouped["validation"]},
            {
                "exclude-reference",
                "invalid-selection",
                "clarify-product",
                "unobserved-market",
                "change-product",
                "stale-candidates",
            },
        )

    def test_unknown_split_is_rejected(self) -> None:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", encoding="utf-8", delete=False
        ) as handle:
            handle.write('[{"id":"x","split":"production","turns":[]}]')
            path = Path(handle.name)
        try:
            with self.assertRaises(ValueError):
                rsi.load_cases(path)
        finally:
            path.unlink(missing_ok=True)


class CheckTurnTests(unittest.TestCase):
    def test_status_requirements_candidates_and_phrase(self) -> None:
        state = {
            "requirements": {"hs_prefix": "841891", "market_country": "Colombia"},
            "candidates": [{"supplier_id": 20}, {"supplier_id": 10}, {"supplier_id": 30}],
            "comparisons": [],
            "profiles": [],
        }
        expected = {
            "requirements": {"hs_prefix": "841891", "market_country": "Colombia"},
            "candidate_ids": [20, 10, 30],
            "answer_contains": ["未找到候选"],
        }
        checks = rsi.check_turn(expected, outcome(state, text="未找到候选"))
        self.assertTrue(checks["status"])
        self.assertTrue(checks["requirement:hs_prefix"])
        self.assertTrue(checks["candidate_ids"])
        self.assertTrue(checks["answer:未找到候选"])

    def test_topic_checks_profile_content(self) -> None:
        state = {
            "requirements": {},
            "candidates": [],
            "comparisons": [],
            "profiles": [{"buyers": [{"buyer_id": 1}], "monthly_activity": []}],
        }
        checks = rsi.check_turn({"topics": ["customers"]}, outcome(state))
        self.assertTrue(checks["profile_has_buyers"])
        self.assertNotIn("profile_has_activity", checks)

    def test_mismatched_candidate_ids_fail(self) -> None:
        state = {
            "requirements": {},
            "candidates": [{"supplier_id": 1}],
            "comparisons": [],
            "profiles": [],
        }
        checks = rsi.check_turn({"candidate_ids": [2]}, outcome(state))
        self.assertFalse(checks["candidate_ids"])


def row(
    case: str,
    passed: bool,
    turns: list[dict],
    elapsed: float = 0.1,
    repeats: int = 1,
    run_passed: list[bool] | None = None,
) -> dict:
    statuses = list(run_passed) if run_passed is not None else [passed] * repeats
    runs = [{"turns": turns, "passed": status} for status in statuses]
    return {
        "case": case,
        "passed": all(statuses),
        "repeats": repeats,
        "elapsed_s": elapsed,
        "runs": runs,
    }


class SummaryTests(unittest.TestCase):
    def test_pass_rates_and_averages(self) -> None:
        rows = [
            row(
                "a",
                True,
                [
                    {"passed": True, "model_calls": 2, "tool_calls": 2},
                    {"passed": True, "model_calls": 1, "tool_calls": 1},
                ],
            ),
            row("b", False, [{"passed": False, "model_calls": 3, "tool_calls": 2}]),
        ]
        summary = rsi.summarize(rows)
        self.assertEqual(summary["cases"], 2)
        self.assertEqual(summary["cases_passed"], 1)
        self.assertAlmostEqual(summary["case_pass_rate"], 0.5)
        self.assertEqual(summary["turns"], 3)
        self.assertEqual(summary["turns_passed"], 2)
        self.assertEqual(summary["turn_pass_rate"], round(2 / 3, 4))
        self.assertEqual(summary["avg_model_calls_per_case"], 3.0)
        self.assertEqual(summary["avg_tool_calls_per_case"], 2.5)
        self.assertEqual(summary["repeat_pass_rates"], [0.5])
        self.assertEqual(summary["repeat_pass_rate_mean"], 0.5)
        self.assertEqual(summary["repeat_pass_rate_std"], 0.0)
        self.assertEqual(summary["flaky_cases"], [])

    def test_repeat_pass_rates_mean_std_and_flaky(self) -> None:
        rows = [
            row("a", True, [{"passed": True}], repeats=2, run_passed=[True, True]),
            row("b", False, [{"passed": False}], repeats=2, run_passed=[True, False]),
        ]
        summary = rsi.summarize(rows)
        self.assertEqual(summary["repeat_pass_rates"], [1.0, 0.5])
        self.assertEqual(summary["repeat_pass_rate_mean"], 0.75)
        self.assertEqual(summary["repeat_pass_rate_std"], 0.25)
        self.assertEqual(summary["flaky_cases"], ["b"])


class RegressionTests(unittest.TestCase):
    def test_passing_check_that_breaks_is_flagged(self) -> None:
        baseline = [row("a", True, [{"checks": {"candidate_ids": True}}])]
        candidate = [row("a", False, [{"checks": {"candidate_ids": False}}])]
        self.assertEqual(rsi.regressions(baseline, candidate), ["a:turn1:candidate_ids"])

    def test_no_regression_when_both_pass(self) -> None:
        baseline = [row("a", True, [{"checks": {"candidate_ids": True}}])]
        candidate = [row("a", True, [{"checks": {"candidate_ids": True}}])]
        self.assertEqual(rsi.regressions(baseline, candidate), [])


class DecisionTests(unittest.TestCase):
    def split(self, rate: float) -> dict:
        return {"summary": {"case_pass_rate": rate}}

    def test_keep_when_dev_improves_and_validation_holds(self) -> None:
        development = {"baseline": self.split(0.5), "candidate": self.split(0.75)}
        validation = {"baseline": self.split(0.8), "candidate": self.split(0.8)}
        self.assertEqual(rsi.decide(development, validation, [], False), "KEEP")

    def test_reject_when_dev_does_not_improve(self) -> None:
        development = {"baseline": self.split(0.5), "candidate": self.split(0.5)}
        validation = {"baseline": self.split(0.8), "candidate": self.split(0.8)}
        self.assertEqual(rsi.decide(development, validation, [], False), "REJECT")

    def test_reject_on_validation_regression(self) -> None:
        development = {"baseline": self.split(0.5), "candidate": self.split(0.75)}
        validation = {"baseline": self.split(0.8), "candidate": self.split(0.6)}
        self.assertEqual(
            rsi.decide(development, validation, ["a:turn1:candidate_ids"], False), "REJECT"
        )

    def test_smoke_is_neutral_decision(self) -> None:
        self.assertEqual(rsi.decide({}, {}, [], True), "SMOKE_ONLY")


class OfflineEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.snapshot = self.root / "trades.sqlite"
        publish_snapshot(self.snapshot, rsi.synthetic_records(), "SYNTHETIC RSI offline test")
        self.case = next(
            case for case in rsi.load_cases(rsi.FIXTURE)["development"] if case["id"] == "discover"
        )

    def test_discover_case_passes_with_scripted_model(self) -> None:
        model = ScriptedModel(
            [
                reply("set_requirements", {"market_country": "Colombia"}),
                reply("find_supplier_candidates"),
                reply("submit_findings"),
            ]
        )
        result = rsi.run_case_repeated(
            self.case, 1, rsi.CANDIDATE, model, self.snapshot, self.root / "run"
        )
        self.assertTrue(result["passed"], result["runs"])
        state = result["runs"][0]["turns"][0]
        self.assertTrue(state["checks"]["candidate_ids"])

    def test_repeat_case_fails_if_one_run_fails(self) -> None:
        first = [
            reply("set_requirements", {"market_country": "Colombia"}),
            reply("find_supplier_candidates"),
            reply("submit_findings"),
        ]
        second = [ModelReply(text="调查中"), ModelReply(text="继续调查")]
        model = ScriptedModel([*first, *second])
        result = rsi.run_case_repeated(
            self.case, 2, rsi.CANDIDATE, model, self.snapshot, self.root / "repeat"
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["repeats"], 2)


if __name__ == "__main__":
    unittest.main()
