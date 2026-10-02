"""从 .env 和进程环境读取配置，进程环境优先。"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    app_id: str
    app_secret: str = field(repr=False)
    host: str = "127.0.0.1"
    port: int = 8080
    db_path: Path = Path("data/qqbot.sqlite3")
    accept_group_messages: bool = False
    log_level: str = "INFO"
    dry_run: bool = False

    @classmethod
    def load(cls, *, dry_run: bool = False) -> "Settings":
        load_dotenv(Path.cwd() / ".env", override=False, encoding="utf-8-sig")
        app_id = os.getenv("QQ_APP_ID", "").strip()
        secret = os.getenv("QQ_APP_SECRET", "").strip()
        host = os.getenv("QQ_HOST", "127.0.0.1").strip()
        if dry_run:
            if host not in {"127.0.0.1", "localhost", "::1"}:
                raise ConfigurationError("--dry-run 只允许本机监听，请将 QQ_HOST 设为 127.0.0.1")
            app_id = app_id or "local-demo"
            secret = secret or "local-demo-secret"
        elif not app_id or not secret:
            raise ConfigurationError("请在项目 .env 中填写 QQ_APP_ID 和 QQ_APP_SECRET，再启动服务")
        try:
            port = int(os.getenv("QQ_PORT", "8080"))
        except ValueError:
            raise ConfigurationError("QQ_PORT 必须是整数") from None
        if not 1 <= port <= 65535:
            raise ConfigurationError("QQ_PORT 必须在 1～65535 之间")
        flag = os.getenv("QQ_ACCEPT_GROUP_MESSAGES", "false").strip().lower()
        if flag not in {"true", "false"}:
            raise ConfigurationError("QQ_ACCEPT_GROUP_MESSAGES 必须为 true 或 false")
        level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("LOG_LEVEL 应为 DEBUG、INFO、WARNING、ERROR 或 CRITICAL")
        db_path = Path(os.getenv("QQ_DB_PATH", "data/qqbot.sqlite3"))
        if dry_run:
            db_path = db_path.with_name(f"{db_path.stem}.dry-run{db_path.suffix}")
        return cls(
            app_id=app_id,
            app_secret=secret,
            host=host,
            port=port,
            db_path=db_path,
            accept_group_messages=flag == "true",
            log_level=level,
            dry_run=dry_run,
        )
