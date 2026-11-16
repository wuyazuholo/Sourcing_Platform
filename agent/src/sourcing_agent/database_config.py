"""Explicit SQL Server settings. Credentials are never included in printable metadata."""

import os
from typing import Literal

from pydantic import Field, SecretStr, ValidationError

from sourcing_agent.harness.contracts import Record


class DatabaseConfig(Record):
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=1433, ge=1, le=65535)
    database: str = Field(default="XpressfeedDB", min_length=1, max_length=128)
    user: SecretStr = Field(exclude=True, repr=False)
    password: SecretStr = Field(exclude=True, repr=False)
    encrypt: Literal["yes", "no", "strict"] = "yes"
    trust_server_certificate: Literal["yes", "no"] = "no"
    login_timeout: int = Field(default=5, ge=1, le=30)
    query_timeout: int = Field(default=15, ge=1, le=120)


def database_config() -> DatabaseConfig:
    try:
        user, password = os.getenv("SOURCING_DB_USER", ""), os.getenv("SOURCING_DB_PASSWORD", "")
        if not user or not password:
            raise ValueError("Missing local credentials")
        return DatabaseConfig(
            host=os.getenv("SOURCING_DB_HOST", ""),
            port=os.getenv("SOURCING_DB_PORT", "1433"),
            database=os.getenv("SOURCING_DB_NAME", "XpressfeedDB"),
            user=SecretStr(user),
            password=SecretStr(password),
            encrypt=os.getenv("SOURCING_DB_ENCRYPT", "yes"),
            trust_server_certificate=os.getenv("SOURCING_DB_TRUST_SERVER_CERTIFICATE", "no"),
            login_timeout=os.getenv("SOURCING_DB_LOGIN_TIMEOUT", "5"),
            query_timeout=os.getenv("SOURCING_DB_QUERY_TIMEOUT", "15"),
        )
    except (ValueError, ValidationError):
        raise ValueError(
            "数据库配置缺失或无效，请在指定本机环境文件中填写 SOURCING_DB_*；不要在聊天中发送密码"
        ) from None
