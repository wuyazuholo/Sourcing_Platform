"""明确标识的离线演示模型；按工具状态演示流程，不用于真实自然语言推理。"""

import json
from uuid import uuid4

from sourcing_agent.harness.contracts import ModelReply, ModelRequest, ToolCall


class DemoModel:
    def generate(self, request: ModelRequest) -> ModelReply:
        if request.purpose == "summary":
            return ModelReply(
                text="先前交互已归档；以当前结构化条件和证据引用为准，不沿用失效候选。"
            )
        state = json.loads(request.messages[0].content)["effective_task_state"]
        if state["missing"]:
            name, arguments = "ask_user", {"question": "请提供商品 HS 编码和明确起止日期。"}
        elif not state["queried"]:
            name, arguments = "find_supplier_candidates", {}
        elif len(state["candidate_ids"]) >= 2 and not state["comparison_count"]:
            name, arguments = (
                "compare_candidates",
                {
                    "supplier_ids": state["candidate_ids"][:2],
                    "metric": "buyer_count",
                },
            )
        else:
            name, arguments = "submit_findings", {}
        return ModelReply(
            calls=[ToolCall(id=uuid4().hex, name=name, arguments=arguments)], finish="tool_calls"
        )
