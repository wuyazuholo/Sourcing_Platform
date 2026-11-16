"""SDK 无关的模型、工具与任务契约。"""

from collections.abc import Callable
from threading import Event
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue

type JsonObject = dict[str, JsonValue]
type EngineName = Literal["native", "langgraph"]
type RunStatus = Literal["completed", "waiting_user", "budget_exhausted", "failed", "canceled"]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolCall(Record):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: JsonObject = Field(default_factory=dict)


class Message(Record):
    role: Literal["user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    provider_data: JsonObject = Field(default_factory=dict)


class ToolDefinition(Record):
    name: str
    description: str
    parameters: JsonObject


class ModelRequest(Record):
    system: str
    messages: list[Message]
    tools: list[ToolDefinition] = Field(default_factory=list)
    max_output_tokens: int = Field(default=2048, gt=0)
    purpose: Literal["research", "summary"] = "research"


class ModelReply(Record):
    text: str = ""
    calls: list[ToolCall] = Field(default_factory=list)
    finish: Literal["stop", "tool_calls", "length"] = "stop"
    provider_data: JsonObject = Field(default_factory=dict)


class ChatModel(Protocol):
    def generate(self, request: ModelRequest) -> ModelReply: ...


class TaskOutcome(Record):
    status: Literal["completed", "waiting_user"]
    text: str


class AgentTask(Protocol):
    def definitions(self) -> list[ToolDefinition]: ...
    def execute(self, name: str, arguments: JsonObject) -> JsonObject: ...
    def briefing(self) -> JsonObject: ...
    def checkpoint(self) -> JsonObject: ...
    def outcome(self) -> TaskOutcome | None: ...
    def render(self) -> str: ...


class RunOutcome(Record):
    status: RunStatus
    text: str
    messages: list[Message]
    task_state: JsonObject
    summary: str = ""
    model_calls: int = 0
    tool_calls: int = 0
    summary_calls: int = 0


class RunLimits(Record):
    model_calls: int = Field(default=10, ge=1, le=100)
    tool_calls: int = Field(default=16, ge=1, le=200)
    max_output_tokens: int = Field(default=2048, ge=128)


class ToolInputError(ValueError):
    """可反馈给模型修正的工具输入错误。"""


class RunCanceled(Exception):
    """取消不会提交本轮业务状态。"""


class Cancellation:
    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        self._event.set()

    def check(self) -> None:
        if self._event.is_set():
            raise RunCanceled("运行已取消")


type EventWriter = Callable[[str, JsonObject], None]
