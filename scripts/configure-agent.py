"""Initialize ignored local bridge settings; never print or overwrite credentials."""
import argparse
import secrets
from pathlib import Path

from dotenv import dotenv_values

from sourcing_agent.config import load_environment
from sourcing_agent.database_config import database_config

parser = argparse.ArgumentParser()
parser.add_argument("--agent-root", type=Path, required=True)
args = parser.parse_args()
platform = Path(__file__).resolve().parents[1]
agent = args.agent_root.resolve(strict=True)
environment = agent / ".env.platform.local"
settings = platform / "backend/data/agent-local.properties"
if environment.exists():
    token = dotenv_values(environment, interpolate=False).get("SOURCING_AGENT_API_TOKEN", "")
    if not token or len(token) < 32:
        raise SystemExit("已有 Agent 服务凭据无效，请检查本机配置；未覆盖文件。")
else:
    token = secrets.token_urlsafe(48)
    if settings.exists():
        raise SystemExit("平台已有桥接配置，请先核对与 Agent 是否一致；未覆盖文件。")
    with environment.open("x", encoding="utf-8") as target:
        target.write('SOURCING_AGENT_API_TOKEN="' + token + '"\n')

settings.parent.mkdir(parents=True, exist_ok=True)
if settings.exists():
    entries = dict(line.split("=", 1) for line in settings.read_text(encoding="utf-8").splitlines() if "=" in line and not line.lstrip().startswith("#"))
    if entries.get("agent.token") != token:
        raise SystemExit("两个项目的桥接凭据不一致；未覆盖配置。")
else:
    with settings.open("x", encoding="utf-8") as target:
        target.write("agent.base-url=http://127.0.0.1:8090\nagent.token=" + token + "\n")

vendor = platform / "backend/data/vendor-local.properties"
if not vendor.exists():
    load_environment(agent / ".env.database.local")
    config = database_config()
    def property_value(value):
        result = ""
        for character in value:
            if character in "\\\n\r\t\f":
                result += {"\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\f": "\\f"}[character]
            elif ord(character) > 127:
                data = character.encode("utf-16-be")
                result += "".join("\\u" + data[index:index+2].hex() for index in range(0, len(data), 2))
            else:
                result += character
        return result
    encryption = {"yes": "true", "no": "false", "strict": "strict"}[config.encrypt]
    trust = "true" if config.trust_server_certificate == "yes" else "false"
    url = f"jdbc:sqlserver://{config.host}:{config.port};databaseName={config.database};encrypt={encryption};trustServerCertificate={trust};loginTimeout={config.login_timeout};socketTimeout=20000;"
    entries = {"vendor.enabled": "true", "vendor.datasource.url": url, "vendor.datasource.username": config.user.get_secret_value(), "vendor.datasource.password": config.password.get_secret_value(), "vendor.datasource.driver-class-name": "com.microsoft.sqlserver.jdbc.SQLServerDriver"}
    with vendor.open("x", encoding="ascii") as target:
        target.write("\n".join(key + "=" + property_value(value) for key, value in entries.items()) + "\n")
print("Local bridge configuration is ready; credentials were not displayed.")
