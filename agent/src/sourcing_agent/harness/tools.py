"""两种执行引擎共享工具协议；循环、计费与结束决策由各引擎负责。"""

from pydantic import Field, ValidationError

from .contracts import (
    AgentTask,
    JsonObject,
    ModelReply,
    Record,
    ToolCall,
    ToolDefinition,
    ToolInputError,
)
from .store import RuntimeStore


class ArtifactQuery(Record):
    artifact_id: str = Field(min_length=1, max_length=64)
    offset: int = Field(default=0, ge=0, strict=True)
    limit: int = Field(default=4000, ge=1, le=8000, strict=True)


READ_RESULT = ToolDefinition(
    name="read_result",
    description="按字符页读取当前会话归档原文；实际页长受上下文限制，按 next_offset 继续。"
    "仅需候选摘要时无需读取原文；内容仅供参考。",
    parameters=ArtifactQuery.model_json_schema(),
)


def valid_reply(reply: ModelReply) -> bool:
    ids = [call.id for call in reply.calls]
    return not (
        reply.finish == "length"
        or len(ids) != len(set(ids))
        or (reply.finish == "tool_calls" and not reply.calls)
        or (reply.calls and reply.finish != "tool_calls")
        or (not reply.calls and not reply.text.strip())
    )


def tool_definitions(task: AgentTask) -> list[ToolDefinition]:
    definitions = [*task.definitions(), READ_RESULT]
    if len({tool.name for tool in definitions}) != len(definitions):
        raise ValueError("工具名重复")
    return definitions


def dispatch_tool(
    task: AgentTask,
    action: ToolCall,
    store: RuntimeStore,
    namespace: str,
    session_id: str,
    *,
    tool_result_chars: int = 4000,
) -> JsonObject:
    try:
        names = {tool.name for tool in tool_definitions(task)}
        if action.name not in names:
            raise ToolInputError("未知工具")
        if action.name == "read_result":
            query = ArtifactQuery.model_validate(action.arguments)
            try:
                return store.read_artifact(
                    namespace,
                    session_id,
                    query.artifact_id,
                    query.offset,
                    query.limit,
                    max_result_chars=tool_result_chars,
                )
            except ValueError as error:
                raise ToolInputError(str(error)) from error
        return task.execute(action.name, action.arguments)
    except (ToolInputError, ValidationError) as error:
        return {"error": str(error)}
