"""显式加载本地配置；环境变量优先，文件中的相对数据路径相对配置文件。"""

import json
import os
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import TypeAdapter

from sourcing_agent.harness.contracts import JsonObject

RESERVED_OPTIONS = {"model", "messages", "tools", "max_tokens", "stream", "api_key", "base_url"}


def load_environment(path: Path | None) -> None:
    if path is None:
        return
    path = path.resolve(strict=True)
    for key, value in dotenv_values(path, interpolate=False).items():
        if value is not None:
            os.environ.setdefault(key, value)


def selected_provider(explicit: str | None) -> Literal["anthropic", "openai"]:
    value = explicit or os.getenv("SOURCING_MODEL_PROVIDER", "anthropic")
    if value not in {"anthropic", "openai"}:
        raise ValueError("SOURCING_MODEL_PROVIDER 只能是 anthropic 或 openai")
    return value


def selected_snapshot(explicit: Path | None, env_file: Path | None) -> Path:
    if explicit is not None:
        return explicit.resolve(strict=True)
    value = os.getenv("SOURCING_TRADE_SNAPSHOT")
    if not value:
        raise ValueError("请提供 --snapshot，或在配置中设置 SOURCING_TRADE_SNAPSHOT")
    path = Path(value)
    if not path.is_absolute():
        path = (env_file.resolve().parent if env_file else Path.cwd()) / path
    return path.resolve(strict=True)


def request_options() -> JsonObject:
    raw = os.getenv("SOURCING_MODEL_REQUEST_OPTIONS", "{}")
    try:
        options = TypeAdapter(JsonObject).validate_python(json.loads(raw))
    except ValueError as error:
        raise ValueError("SOURCING_MODEL_REQUEST_OPTIONS 必须是 JSON 对象") from error
    if set(options) & RESERVED_OPTIONS:
        raise ValueError("模型扩展参数不能覆盖消息、工具、模型、输出预算或连接配置")
    return options
