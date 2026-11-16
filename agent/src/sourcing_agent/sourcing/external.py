"""External observations are separate from snapshot-backed trade facts."""

from typing import Protocol

from pydantic import Field

from sourcing_agent.harness.contracts import JsonObject, Record, ToolDefinition


class ExternalObservation(Record):
    id: str
    supplier_id: int
    server: str
    tool: str
    retrieved_at: str
    content_sha256: str
    source_urls: list[str] = Field(default_factory=list)
    excerpt: str
    result: JsonObject

    def summary(self) -> JsonObject:
        return self.model_dump(mode="json", exclude={"result"})


class ExternalTools(Protocol):
    identity: str

    def definitions(self) -> list[ToolDefinition]: ...

    def call(self, name: str, arguments: JsonObject, supplier_id: int) -> ExternalObservation: ...
