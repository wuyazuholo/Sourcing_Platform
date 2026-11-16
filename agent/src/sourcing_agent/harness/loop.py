"""供应商任务共享的显式 Agent Loop；业务规则由 AgentTask 提供。"""

from .context import ContextManager
from .contracts import (
    AgentTask,
    Cancellation,
    ChatModel,
    EventWriter,
    JsonObject,
    Message,
    RunLimits,
    RunOutcome,
    RunStatus,
)
from .store import Memory, RuntimeStore, encode
from .tools import dispatch_tool, tool_definitions, valid_reply


class AgentLoop:
    def __init__(
        self,
        model: ChatModel,
        context: ContextManager,
        store: RuntimeStore,
        namespace: str,
        session_id: str,
        emit: EventWriter,
        limits: RunLimits,
        cancellation: Cancellation,
    ) -> None:
        self.model = model
        self.context = context
        self.store = store
        self.namespace = namespace
        self.session_id = session_id
        self.emit = emit
        self.limits = limits
        self.cancellation = cancellation

    def run(
        self,
        task: AgentTask,
        system: str,
        messages: list[Message],
        summary: str,
        memories: list[Memory],
    ) -> RunOutcome:
        model_calls = tool_calls = summary_calls = 0
        definitions = tool_definitions(task)

        def outcome(status: RunStatus, text: str) -> RunOutcome:
            return RunOutcome(
                status=status,
                text=text,
                messages=[*messages, Message(role="assistant", content=text)],
                task_state=task.checkpoint(),
                summary=summary,
                model_calls=model_calls,
                tool_calls=tool_calls,
                summary_calls=summary_calls,
            )

        while model_calls < self.limits.model_calls:
            self.cancellation.check()
            prepared = self.context.prepare(
                self.model,
                system,
                messages,
                summary,
                task.briefing(),
                memories,
                definitions,
                self.limits.max_output_tokens,
                self.context.policy.summary_calls_per_run - summary_calls,
            )
            messages, summary = prepared.messages, prepared.summary
            summary_calls += prepared.summary_calls
            self.cancellation.check()
            self.emit("model.request", prepared.request.model_dump(mode="json"))
            model_calls += 1
            reply = self.model.generate(prepared.request)
            self.emit("model.response", reply.model_dump(mode="json"))
            self.cancellation.check()

            if not valid_reply(reply):
                self.emit("model.rejected", {"reason": "incomplete_or_invalid_protocol"})
                messages.append(
                    Message(
                        role="user", content="上次输出不完整或协议无效，请重新给出完整工具调用。"
                    )
                )
                continue

            messages.append(
                Message(
                    role="assistant",
                    content=reply.text,
                    tool_calls=reply.calls,
                    provider_data=reply.provider_data,
                )
            )
            if not reply.calls:
                # 用户可见结论由实际数据投影，模型草稿不能补造企业、指标或已满足条件。
                messages.append(
                    Message(
                        role="user",
                        content="请调用 submit_findings 提交经工具核实的结果；缺条件时调用 ask_user。",
                    )
                )
                continue

            for action in reply.calls:
                self.cancellation.check()
                self.emit("tool.started", action.model_dump(mode="json"))
                if tool_calls >= self.limits.tool_calls:
                    result: JsonObject = {"error": "工具预算已耗尽，本动作未执行"}
                else:
                    tool_calls += 1
                    result = dispatch_tool(
                        task,
                        action,
                        self.store,
                        self.namespace,
                        self.session_id,
                        tool_result_chars=self.context.policy.tool_result_chars,
                    )
                self.cancellation.check()
                self.emit("tool.result", {"tool_call_id": action.id, "result": result})
                messages.append(
                    Message(role="tool", content=encode(result), tool_call_id=action.id)
                )

            # 全批工具都回填后才能暂停或提交，避免留下不配对的 tool_call_id。
            if terminal := task.outcome():
                return outcome(terminal.status, terminal.text)
            if tool_calls >= self.limits.tool_calls:
                return outcome(
                    "budget_exhausted", task.render() + "\n\n工具预算耗尽，调查尚未提交。"
                )

        return outcome("budget_exhausted", task.render() + "\n\n模型调用预算耗尽，调查尚未提交。")
