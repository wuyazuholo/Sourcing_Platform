"""统一会话提交边界；原生版重跑回合，图引擎从持久化节点恢复。"""

from collections.abc import Callable

from .context import ContextManager, ContextPolicy
from .contracts import (
    AgentTask,
    Cancellation,
    ChatModel,
    EngineName,
    JsonObject,
    Message,
    RunCanceled,
    RunLimits,
    RunOutcome,
)
from .loop import AgentLoop
from .memory import MemoryService
from .store import RuntimeStore, Session

type TaskFactory = Callable[[JsonObject, str], AgentTask]


class SessionRunner:
    def __init__(
        self,
        store: RuntimeStore,
        model: ChatModel,
        task_factory: TaskFactory,
        system: str,
        namespace: str,
        snapshot_sha256: str,
        policy: ContextPolicy | None = None,
        limits: RunLimits | None = None,
        verify_source: Callable[[], None] | None = None,
        engine: EngineName = "native",
    ) -> None:
        self.store = store
        self.model = model
        self.task_factory = task_factory
        self.system = system
        self.namespace = namespace
        self.snapshot_sha256 = snapshot_sha256
        self.policy = policy or ContextPolicy()
        self.limits = limits or RunLimits()
        self.verify_source = verify_source
        if engine not in {"native", "langgraph"}:
            raise ValueError("未知执行引擎")
        self.engine = engine

    def create_session(self) -> Session:
        return self.store.create_session(self.namespace, self.snapshot_sha256, self.engine)

    def run(
        self,
        session_id: str,
        user_message: str | None = None,
        *,
        resume: bool = False,
        cancellation: Cancellation | None = None,
    ) -> RunOutcome:
        session = self.store.load_session(session_id, self.namespace)
        if session.snapshot_sha256 != self.snapshot_sha256:
            raise ValueError("数据版本已变化，请创建新会话")
        if session.engine != self.engine:
            raise ValueError(f"会话属于 {session.engine} 引擎，请使用相同 --engine 或创建新会话")
        if session.status == "running":
            raise ValueError("会话仍为 running；确认旧进程停止后，显式 session recover")
        if resume:
            if (
                session.status not in {"failed", "canceled", "interrupted"}
                or not session.pending_input
            ):
                raise ValueError("只有失败、取消或中断的输入可以重跑")
            if user_message is not None:
                raise ValueError("resume 不接受新输入")
            user_message = session.pending_input
        elif session.pending_input is not None:
            raise ValueError("请先 resume 未完成输入，或创建新会话")
        if not user_message or not user_message.strip():
            raise ValueError("用户输入不能为空")
        if self.verify_source:
            self.verify_source()
        task = self.task_factory(session.state, user_message)
        memories = MemoryService(self.store, self.namespace).recall(user_message)
        run_id = self.store.begin_run(session, user_message)

        def emit(kind: str, payload: JsonObject) -> None:
            self.store.append_event(session.id, run_id, kind, payload)

        token = cancellation or Cancellation()
        context = ContextManager(
            self.policy, self.store, self.namespace, session.id, emit, token.check
        )
        try:
            messages = [*session.messages, Message(role="user", content=user_message)]
            if self.engine == "native":
                loop = AgentLoop(
                    self.model,
                    context,
                    self.store,
                    self.namespace,
                    session.id,
                    emit,
                    self.limits,
                    token,
                )
                result = loop.run(task, self.system, messages, session.summary, memories)
            else:
                from .graph import LangGraphLoop

                graph_loop = LangGraphLoop(
                    self.model,
                    context,
                    self.store,
                    self.namespace,
                    session.id,
                    emit,
                    self.limits,
                    token,
                    self.task_factory,
                    self.store.path.with_name("langgraph-checkpoints.sqlite"),
                    self.system,
                    memories,
                )
                result = graph_loop.run(
                    task,
                    messages,
                    session.summary,
                    user_message,
                    self.store.turn_id(session.id, run_id),
                    resume,
                )
            token.check()
            if self.verify_source:
                self.verify_source()
            self.store.commit_run(session, run_id, result)
            return result
        except (RunCanceled, KeyboardInterrupt) as error:
            self.store.fail_run(session, run_id, "canceled", str(error) or "用户中断")
            raise
        except Exception as error:
            self.store.fail_run(session, run_id, "failed", str(error))
            raise
