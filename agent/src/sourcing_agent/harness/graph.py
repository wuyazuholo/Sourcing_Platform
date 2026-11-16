"""真正的 StateGraph 执行：节点负责单步操作，条件边负责循环。"""

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Literal, TypedDict

from pydantic import Field

try:
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command, interrupt
except ImportError as error:
    raise RuntimeError("LangGraph 版本需要安装：pip install -e .[graph]") from error

from .context import ContextManager
from .contracts import (
    AgentTask,
    Cancellation,
    ChatModel,
    EventWriter,
    JsonObject,
    Message,
    ModelReply,
    ModelRequest,
    Record,
    RunCanceled,
    RunLimits,
    RunOutcome,
    RunStatus,
    TaskOutcome,
)
from .store import Memory, RuntimeStore, encode
from .tools import dispatch_tool, tool_definitions, valid_reply

type TaskFactory = Callable[[JsonObject, str], AgentTask]
type Phase = Literal[
    "prepare_context",
    "call_model",
    "execute_tool",
    "assess",
    "await_user",
    "finalize",
    "finished",
    "failed",
]


class GraphExecution(Record):
    format_version: Literal[1] = 1
    turn_id: str
    user_message: str
    messages: list[Message]
    task_state: JsonObject
    summary: str = ""
    model_calls: int = 0
    tool_calls: int = 0
    summary_calls: int = 0
    phase: Phase = "prepare_context"
    request: ModelRequest | None = None
    reply: ModelReply | None = None
    action_index: int = 0
    terminal: TaskOutcome | None = None
    status: RunStatus | None = None
    text: str = ""
    failure_phase: Phase | None = None
    error: str = ""
    canceled: bool = False
    limits: RunLimits = Field(default_factory=RunLimits)


class GraphState(TypedDict):
    # 检查点只保存 JSON，不保存 SDK、连接、闭包或可执行对象。
    execution: JsonObject


class LangGraphLoop:
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
        task_factory: TaskFactory,
        checkpoint_path: Path,
        system: str,
        memories: list[Memory],
    ) -> None:
        self.model = model
        self.context = context
        self.store = store
        self.namespace = namespace
        self.session_id = session_id
        self.emit = emit
        self.limits = limits
        self.cancellation = cancellation
        self.task_factory = task_factory
        self.checkpoint_path = checkpoint_path
        self.system = system
        self.memories = memories

    def _task(self, state: GraphExecution) -> AgentTask:
        return self.task_factory(state.task_state, state.user_message)

    def _prepare(self, state: GraphExecution) -> GraphExecution:
        self.cancellation.check()
        if state.model_calls >= state.limits.model_calls:
            state.status, state.text = (
                "budget_exhausted",
                (self._task(state).render() + "\n\n模型调用预算耗尽，调查尚未提交。"),
            )
            state.phase = "finalize"
            return state
        task = self._task(state)
        prepared = self.context.prepare(
            self.model,
            self.system,
            state.messages,
            state.summary,
            task.briefing(),
            self.memories,
            tool_definitions(task),
            state.limits.max_output_tokens,
            self.context.policy.summary_calls_per_run - state.summary_calls,
        )
        state.messages, state.summary = prepared.messages, prepared.summary
        state.summary_calls += prepared.summary_calls
        state.request = prepared.request
        state.phase = "call_model"
        return state

    def _call(self, state: GraphExecution) -> GraphExecution:
        self.cancellation.check()
        if state.request is None:
            raise ValueError("图状态缺少模型请求")
        state.model_calls += 1
        self.emit("model.request", state.request.model_dump(mode="json"))
        reply = self.model.generate(state.request)
        self.emit("model.response", reply.model_dump(mode="json"))
        self.cancellation.check()
        state.reply = reply
        if not valid_reply(reply):
            self.emit("model.rejected", {"reason": "incomplete_or_invalid_protocol"})
            state.messages.append(
                Message(role="user", content="上次输出不完整或协议无效，请重新给出完整工具调用。")
            )
            state.phase = "prepare_context"
            return state
        state.messages.append(
            Message(
                role="assistant",
                content=reply.text,
                tool_calls=reply.calls,
                provider_data=reply.provider_data,
            )
        )
        if not reply.calls:
            state.messages.append(
                Message(
                    role="user",
                    content="请调用 submit_findings 提交经工具核实的结果；缺条件时调用 ask_user。",
                )
            )
            state.phase = "prepare_context"
        else:
            state.action_index = 0
            state.phase = "execute_tool"
        return state

    def _execute_tool(self, state: GraphExecution) -> GraphExecution:
        self.cancellation.check()
        if state.reply is None or state.action_index >= len(state.reply.calls):
            raise ValueError("图状态缺少待执行工具")
        action = state.reply.calls[state.action_index]
        self.emit("tool.started", action.model_dump(mode="json"))
        task = self._task(state)
        if state.tool_calls >= state.limits.tool_calls:
            result: JsonObject = {"error": "工具预算已耗尽，本动作未执行"}
        else:
            state.tool_calls += 1
            # 前一节点可能已追问/提交；同批剩余动作仍逐一回填，但不再改变业务状态。
            if state.terminal is not None and action.name != "read_result":
                result = {"error": "本轮已提出追问或提交结果，不再执行后续业务动作"}
            else:
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
        state.messages.append(Message(role="tool", content=encode(result), tool_call_id=action.id))
        state.task_state = task.checkpoint()
        state.terminal = state.terminal or task.outcome()
        state.action_index += 1
        state.phase = "execute_tool" if state.action_index < len(state.reply.calls) else "assess"
        return state

    def _assess(self, state: GraphExecution) -> GraphExecution:
        if state.terminal:
            state.status, state.text = state.terminal.status, state.terminal.text
            state.phase = "await_user" if state.status == "waiting_user" else "finalize"
        elif state.tool_calls >= state.limits.tool_calls:
            state.status = "budget_exhausted"
            state.text = self._task(state).render() + "\n\n工具预算耗尽，调查尚未提交。"
            state.phase = "finalize"
        else:
            state.phase = "prepare_context"
        return state

    def _finalize(self, state: GraphExecution) -> GraphExecution:
        self.cancellation.check()
        state.messages.append(Message(role="assistant", content=state.text))
        state.phase = "finished"
        return state

    def _await_user(self, raw: GraphState) -> GraphState:
        state = GraphExecution.model_validate(raw["execution"])
        # interrupt 所在节点会从头重入；调用前没有工具或外部写操作。
        response = interrupt({"question": state.text, "status": "waiting_user"})
        if not isinstance(response, dict):
            raise ValueError("恢复追问需要结构化输入")
        user_message, turn_id = response.get("user_message"), response.get("turn_id")
        if (
            not isinstance(user_message, str)
            or not user_message.strip()
            or not isinstance(turn_id, str)
        ):
            raise ValueError("恢复输入或回合标识无效")
        task = self.task_factory(state.task_state, user_message)
        resumed = GraphExecution(
            turn_id=turn_id,
            user_message=user_message,
            messages=[
                *state.messages,
                Message(role="assistant", content=state.text),
                Message(role="user", content=user_message),
            ],
            task_state=task.checkpoint(),
            summary=state.summary,
            limits=self.limits,
        )
        self.emit("graph.user_resumed", {"turn_id": turn_id})
        return {"execution": resumed.model_dump(mode="json")}

    def _node(
        self, name: Phase, operation: Callable[[GraphExecution], GraphExecution]
    ) -> Callable[[GraphState], GraphState]:
        def run(raw: GraphState) -> GraphState:
            state = GraphExecution.model_validate(raw["execution"])
            self.emit("graph.node.started", {"node": name, "turn_id": state.turn_id})
            try:
                state = operation(state)
            except (RunCanceled, KeyboardInterrupt) as error:
                state.canceled, state.error = True, str(error) or "用户中断"
                state.failure_phase, state.phase = name, "failed"
            except Exception as error:
                state.error = str(error)
                state.failure_phase, state.phase = name, "failed"
            self.emit("graph.node.finished", {"node": name, "next": state.phase})
            return {"execution": state.model_dump(mode="json")}

        return run

    @staticmethod
    def _route(raw: GraphState) -> str:
        phase = raw["execution"]["phase"]
        return END if phase in {"finished", "failed"} else str(phase)

    def _build(self, saver: SqliteSaver):
        builder = StateGraph(GraphState)
        for name, operation in (
            ("prepare_context", self._prepare),
            ("call_model", self._call),
            ("execute_tool", self._execute_tool),
            ("assess", self._assess),
            ("finalize", self._finalize),
        ):
            builder.add_node(name, self._node(name, operation))
        builder.add_node("await_user", self._await_user)
        builder.add_edge(START, "prepare_context")
        destinations = {
            name: name
            for name in (
                "prepare_context",
                "call_model",
                "execute_tool",
                "assess",
                "await_user",
                "finalize",
            )
        }
        destinations[END] = END
        for name in destinations:
            if name != END:
                builder.add_conditional_edges(name, self._route, destinations)
        return builder.compile(checkpointer=saver)

    @staticmethod
    def _outcome(state: GraphExecution) -> RunOutcome:
        if state.phase == "failed":
            if state.canceled:
                raise RunCanceled(state.error)
            raise RuntimeError(state.error)
        messages = state.messages
        if state.phase == "await_user":
            messages = [*messages, Message(role="assistant", content=state.text)]
        if state.status is None:
            raise ValueError("图未生成终态")
        return RunOutcome(
            status=state.status,
            text=state.text,
            messages=messages,
            task_state=state.task_state,
            summary=state.summary,
            model_calls=state.model_calls,
            tool_calls=state.tool_calls,
            summary_calls=state.summary_calls,
        )

    def run(
        self,
        task: AgentTask,
        messages: list[Message],
        summary: str,
        user_message: str,
        turn_id: str,
        resume: bool,
    ) -> RunOutcome:
        thread_id = hashlib.sha256((self.namespace + "\0" + self.session_id).encode()).hexdigest()
        config = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": self.limits.model_calls * 210 + 100,
        }
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        with SqliteSaver.from_conn_string(str(self.checkpoint_path)) as saver:
            graph = self._build(saver)
            snapshot = graph.get_state(config)
            saved = (
                GraphExecution.model_validate(snapshot.values["execution"])
                if snapshot.values.get("execution")
                else None
            )
            if saved and saved.phase == "await_user":
                if saved.turn_id == turn_id:
                    # 图已追问而应用事务未提交：交付已有追问，不把原输入当补充。
                    return self._outcome(saved)
                result = graph.invoke(
                    Command(resume={"user_message": user_message, "turn_id": turn_id}), config
                )
            elif resume and saved and saved.turn_id == turn_id:
                if saved.limits != self.limits:
                    raise ValueError("恢复时调用预算必须与原回合一致")
                if saved.phase == "finished":
                    return self._outcome(saved)
                if saved.phase == "failed":
                    previous = saved.failure_phase or "prepare_context"
                    saved.phase = "prepare_context" if previous == "call_model" else previous
                    saved.error, saved.canceled, saved.failure_phase = "", False, None
                    # 失败节点已持久化；沿条件边重新调度该节点，不重跑完成的工具节点。
                    graph.update_state(
                        config, {"execution": saved.model_dump(mode="json")}, as_node="call_model"
                    )
                self.emit("graph.resumed", {"turn_id": turn_id, "next": saved.phase})
                result = graph.invoke(None, config)
            else:
                initial = GraphExecution(
                    turn_id=turn_id,
                    user_message=user_message,
                    messages=messages,
                    task_state=task.checkpoint(),
                    summary=summary,
                    limits=self.limits,
                )
                result = graph.invoke({"execution": initial.model_dump(mode="json")}, config)
            state = GraphExecution.model_validate(result["execution"])
            return self._outcome(state)
