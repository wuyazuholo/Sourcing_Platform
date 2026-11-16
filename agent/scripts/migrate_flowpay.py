"""复制旧项目的业务资产与本地模型配置；不复制密钥到日志或 Git 文件。"""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import tomllib
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def copy_asset(source: Path, destination: Path) -> dict[str, object]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    expected = sha256(source)
    if destination.exists():
        if sha256(destination) != expected:
            raise ValueError("目标资产已存在且内容不同：" + str(destination))
    else:
        fd, temporary_name = tempfile.mkstemp(dir=destination.parent, suffix=".copy")
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            shutil.copyfile(source, temporary)
            if sha256(temporary) != expected or sha256(source) != expected:
                raise ValueError("复制期间源文件改变：" + str(source))
            if os.name == "nt":
                os.rename(temporary, destination)
            else:
                os.link(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    return {
        "source": str(source),
        "destination": str(destination),
        "bytes": destination.stat().st_size,
        "sha256": expected,
        "verified": True,
    }


def backup_registry(source: Path, destination: Path) -> dict[str, object]:
    if destination.exists():
        return {
            "source": str(source),
            "destination": str(destination),
            "sha256": sha256(destination),
            "mode": "archive_only",
            "reused": True,
        }
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(dir=destination.parent, suffix=".sqlite")
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(temporary)) as dst:
                src.backup(dst)
        if os.name == "nt":
            os.rename(temporary, destination)
        else:
            os.link(temporary, destination)
        return {
            "source": str(source),
            "destination": str(destination),
            "sha256": sha256(destination),
            "mode": "archive_only",
        }
    finally:
        temporary.unlink(missing_ok=True)


def migrate(source_root: Path, target_root: Path) -> dict[str, object]:
    source_root, target_root = source_root.resolve(strict=True), target_root.resolve(strict=True)
    if source_root == target_root:
        raise ValueError("源目录和目标目录必须不同")
    if not (target_root / "src/sourcing_agent").is_dir():
        raise ValueError("目标目录不是 sourcing-agent 应用")
    source_data = source_root / "var/supply-chain"
    raw_file, snapshot = source_data / "coimport.json", source_data / "trades.sqlite"
    if not raw_file.is_file() or not snapshot.is_file():
        raise ValueError("缺少旧项目原始 JSON 或 trades.sqlite")
    if Path(str(snapshot) + "-wal").exists():
        raise ValueError("交易快照存在 WAL，请先发布独立的完整快照")
    model = tomllib.loads(
        (source_root / "backend/configs/supply-chain.toml").read_text(encoding="utf-8")
    )["model"]
    source_env = dotenv_values(source_root / ".env", interpolate=False)
    key = source_env.get(model["api_key_env"]) or os.getenv(model["api_key_env"])
    if not key or not key.strip():
        raise ValueError("旧项目的模型密钥未配置")
    provider = "openai"
    config = {
        "SOURCING_MODEL_PROVIDER": provider,
        "SOURCING_MODEL_API_KEY": key,
        "SOURCING_MODEL_NAME": source_env.get("FLOWPAY_MODEL_NAME") or model["name"],
        "SOURCING_MODEL_BASE_URL": source_env.get("FLOWPAY_MODEL_BASE_URL") or model["base_url"],
        "SOURCING_MODEL_REQUEST_OPTIONS": json.dumps(
            {k: v for k, v in model.get("request_options", {}).items() if k != "max_tokens"},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "SOURCING_MODEL_MAX_OUTPUT_TOKENS": str(
            model.get("request_options", {}).get("max_tokens", 2048)
        ),
        "SOURCING_TRADE_SNAPSHOT": ".runtime/data/trades.sqlite",
    }
    environment_file = target_root / ".env.local"
    if environment_file.exists():
        existing = dotenv_values(environment_file, interpolate=False)
        if any(existing.get(name) != value for name, value in config.items()):
            raise ValueError("目标 .env.local 已存在且配置不同，未覆盖任何凭据")
    assets = [
        copy_asset(snapshot, target_root / ".runtime/data/trades.sqlite"),
        copy_asset(raw_file, target_root / ".runtime/data/coimport.json"),
    ]
    archive = target_root / ".runtime/imports/flowpay"
    for name in ("evaluation_cases.json", "benchmark_cases.json"):
        source = source_root / "scripts/supply_chain" / name
        if source.exists():
            assets.append({**copy_asset(source, archive / name), "mode": "reference_cases"})
    for pattern in ("review-pack-*.json", "real-investigation-*.md"):
        for source in sorted(source_data.glob(pattern)):
            assets.append({**copy_asset(source, archive / source.name), "mode": "archive_only"})
    registry = source_data / "skills/registry.sqlite"
    if registry.exists():
        assets.append(backup_registry(registry, archive / "skills-registry.sqlite"))
    if not environment_file.exists():
        with environment_file.open("x", encoding="utf-8", newline="\n") as output:
            output.write("# 本机配置；已由 .gitignore 排除。环境变量优先。\n")
            for name, value in config.items():
                output.write(name + "=" + json.dumps(value, ensure_ascii=False) + "\n")
    with closing(sqlite3.connect(snapshot.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        count, start, end = db.execute(
            "SELECT COUNT(*),MIN(shipment_date),MAX(shipment_date) FROM trades"
        ).fetchone()
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "source_root": str(source_root),
        "target_root": str(target_root),
        "assets": assets,
        "row_count": count,
        "date_range": [start, end],
        "model": {
            "provider": provider,
            "name": config["SOURCING_MODEL_NAME"],
            "base_url": config["SOURCING_MODEL_BASE_URL"],
            "key_configured": True,
            "env_file": str(environment_file),
        },
        "excluded": ["临时 SQLite 文件", "旧会话和基准运行日志", "旧服务数据库连接凭据"],
        "archived_not_active": ["旧策略注册库", "旧评测与调查报告"],
    }
    report_file = target_root / ".runtime/migration/manifest.json"
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="迁移旧 FlowPay 供应商数据与本地模型配置")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--target-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    result = migrate(args.source_root, args.target_root)
    print(
        json.dumps(
            {
                "assets": len(result["assets"]),
                "row_count": result["row_count"],
                "date_range": result["date_range"],
                "model": result["model"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
