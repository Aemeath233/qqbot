"""从 .env 和进程环境读取配置，进程环境优先。"""

import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

from qqbot.personas import LENGTHS, PRESETS


class ConfigurationError(ValueError):
    pass


def env_bool(name: str, default: bool = False, *, values: Mapping[str, str] | None = None) -> bool:
    value = (os.environ if values is None else values).get(name, str(default)).strip().lower()
    if value not in {"true", "false"}:
        raise ConfigurationError(f"{name} 必须为 true 或 false")
    return value == "true"


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
    llm_enabled: bool = False
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = field(default="", repr=False)
    llm_model: str = ""
    llm_timeout: float = 30
    electricity_enabled: bool = False
    electricity_school_code: str = "1402"
    electricity_pay_project: int = 953
    electricity_map_path: Path | None = None
    electricity_token: str = field(default="", repr=False)
    electricity_cookie: str = field(default="", repr=False)
    electricity_tapp_id: str = field(default="", repr=False)
    electricity_default_area: str = ""
    electricity_endpoint: str = (
        "https://cloudpaygateway.59wanmei.com/paygateway/smallpaygateway/trade"
    )
    bot_name: str = "小电"
    bot_persona: str = "cat"
    bot_persona_custom: str = ""
    bot_catchphrase: str = ""
    bot_reply_length: str = "balanced"
    bot_group_personas: dict[str, str] = field(default_factory=dict, repr=False)
    memory_enabled: bool = True
    games_enabled: bool = True
    skills_enabled: bool = True
    skills_dir: Path = Path("data/skills")
    toolpacks_enabled: bool = True
    toolpacks_dir: Path = Path("data/toolpacks")
    electricity_history_enabled: bool = True
    electricity_history_retention_days: int = 365

    @classmethod
    def load(cls, *, dry_run: bool = False, require_qq: bool = True) -> "Settings":
        load_dotenv(Path.cwd() / ".env", override=False, encoding="utf-8-sig", interpolate=False)
        return cls.from_values(os.environ, dry_run=dry_run, require_qq=require_qq)

    @classmethod
    def from_values(
        cls, values: Mapping[str, str], *, dry_run: bool = False, require_qq: bool = True
    ) -> "Settings":
        """校验给定配置，不修改进程环境；供管理页及连接测试使用。"""
        app_id = values.get("QQ_APP_ID", "").strip()
        secret = values.get("QQ_APP_SECRET", "").strip()
        host = values.get("QQ_HOST", "127.0.0.1").strip()
        if dry_run:
            if host not in {"127.0.0.1", "localhost", "::1"}:
                raise ConfigurationError("--dry-run 只允许本机监听，请将 QQ_HOST 设为 127.0.0.1")
            app_id = app_id or "local-demo"
            secret = secret or "local-demo-secret"
        elif require_qq and (not app_id or not secret):
            raise ConfigurationError("请在项目 .env 中填写 QQ_APP_ID 和 QQ_APP_SECRET，再启动服务")
        try:
            port = int(values.get("QQ_PORT", "8080"))
        except ValueError:
            raise ConfigurationError("QQ_PORT 必须是整数") from None
        if not 1 <= port <= 65535:
            raise ConfigurationError("QQ_PORT 必须在 1～65535 之间")
        accept_group = env_bool("QQ_ACCEPT_GROUP_MESSAGES", values=values)
        level = values.get("LOG_LEVEL", "INFO").strip().upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("LOG_LEVEL 应为 DEBUG、INFO、WARNING、ERROR 或 CRITICAL")
        db_path = Path(values.get("QQ_DB_PATH", "data/qqbot.sqlite3"))
        if dry_run:
            db_path = db_path.with_name(f"{db_path.stem}.dry-run{db_path.suffix}")
        llm_enabled = env_bool("LLM_ENABLED", values=values) and not dry_run
        llm_base_url = values.get("LLM_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
        llm_api_key = values.get("LLM_API_KEY", "").strip()
        llm_model = values.get("LLM_MODEL", "").strip()
        if llm_enabled:
            if not llm_api_key or not llm_model:
                raise ConfigurationError("启用 LLM 时必须填写 LLM_API_KEY 和 LLM_MODEL")
            url = urlsplit(llm_base_url)
            if (
                not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or url.scheme not in {"https", "http"}
                or (url.scheme == "http" and url.hostname not in {"127.0.0.1", "localhost", "::1"})
            ):
                raise ConfigurationError("LLM_BASE_URL 必须为 HTTPS 地址；本机服务可以使用 HTTP")
        try:
            llm_timeout = float(values.get("LLM_TIMEOUT", "30"))
            pay_project = int(values.get("ELECTRICITY_PAY_PROJECT", "953"))
        except ValueError:
            raise ConfigurationError(
                "LLM_TIMEOUT 必须为数字，ELECTRICITY_PAY_PROJECT 必须为整数"
            ) from None
        if not math.isfinite(llm_timeout) or not 1 <= llm_timeout <= 120 or pay_project <= 0:
            raise ConfigurationError("LLM_TIMEOUT 应在 1～120 秒之间，缴费项目编号必须为正整数")
        electricity_enabled = env_bool("ELECTRICITY_ENABLED", values=values) and not dry_run
        school_code = values.get("ELECTRICITY_SCHOOL_CODE", "1402").strip()
        map_path = values.get("ELECTRICITY_MAP_PATH", "").strip()
        endpoint = values.get("ELECTRICITY_ENDPOINT", cls.electricity_endpoint).strip()
        if electricity_enabled:
            url = urlsplit(endpoint)
            if (
                not url.hostname
                or url.scheme != "https"
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ConfigurationError("ELECTRICITY_ENDPOINT 必须是无凭证和查询参数的 HTTPS 地址")
        if electricity_enabled and (not school_code or (school_code != "1402" and not map_path)):
            raise ConfigurationError(
                "其他学校必须配置对应的 ELECTRICITY_SCHOOL_CODE 和宿舍映射文件"
            )
        name = values.get("BOT_NAME", "小电").strip()
        persona = values.get("BOT_PERSONA", "cat").strip()
        custom = values.get("BOT_PERSONA_CUSTOM", "").strip()
        catchphrase = values.get("BOT_CATCHPHRASE", "").strip()
        length = values.get("BOT_REPLY_LENGTH", "balanced").strip()
        if (
            not 1 <= len(name) <= 32
            or any(ord(c) < 32 for c in name)
            or persona not in PRESETS
            or length not in LENGTHS
            or len(custom) > 2000
            or any(ord(c) < 32 and c not in "\r\n\t" for c in custom)
            or len(catchphrase) > 80
            or any(ord(c) < 32 for c in catchphrase)
        ):
            raise ConfigurationError(
                "请检查机器人名称、人设、回复长度或口头禅；自定义人设最多2000字。"
            )
        group_text = values.get("BOT_GROUP_PERSONAS", "{}").strip() or "{}"
        try:
            groups = json.loads(group_text)
            if (
                len(group_text) > 8000
                or not isinstance(groups, dict)
                or len(groups) > 200
                or any(
                    not isinstance(k, str)
                    or not k.strip()
                    or len(k) > 512
                    or not isinstance(v, str)
                    or v not in PRESETS
                    for k, v in groups.items()
                )
            ):
                raise ValueError
        except (ValueError, TypeError, RecursionError):
            raise ConfigurationError(
                "BOT_GROUP_PERSONAS 应为群标识到cat/friend/gentle/custom的JSON对象。"
            ) from None
        if (persona == "custom" or "custom" in groups.values()) and not custom:
            raise ConfigurationError("使用自定义人格时请填写 BOT_PERSONA_CUSTOM。")
        try:
            retention = int(values.get("ELECTRICITY_HISTORY_RETENTION_DAYS", "365"))
        except ValueError:
            raise ConfigurationError("历史保留天数必须为1～3650的整数。") from None
        if not 1 <= retention <= 3650:
            raise ConfigurationError("历史保留天数必须为1～3650的整数。")
        return cls(
            app_id=app_id,
            app_secret=secret,
            host=host,
            port=port,
            db_path=db_path,
            accept_group_messages=accept_group,
            log_level=level,
            dry_run=dry_run,
            llm_enabled=llm_enabled,
            llm_base_url=llm_base_url,
            llm_api_key=llm_api_key,
            llm_model=llm_model,
            llm_timeout=llm_timeout,
            electricity_enabled=electricity_enabled,
            electricity_school_code=school_code,
            electricity_pay_project=pay_project,
            electricity_map_path=Path(map_path) if map_path else None,
            electricity_token=values.get("ELECTRICITY_TOKEN", "").strip(),
            electricity_cookie=values.get("ELECTRICITY_COOKIE", "").strip(),
            electricity_tapp_id=values.get("ELECTRICITY_TAPP_ID", "").strip(),
            electricity_default_area=values.get("ELECTRICITY_DEFAULT_AREA", "").strip(),
            electricity_endpoint=endpoint,
            bot_name=name,
            bot_persona=persona,
            bot_persona_custom=custom,
            bot_catchphrase=catchphrase,
            bot_reply_length=length,
            bot_group_personas=groups,
            memory_enabled=env_bool("MEMORY_ENABLED", True, values=values),
            games_enabled=env_bool("GAMES_ENABLED", True, values=values),
            skills_enabled=env_bool("SKILLS_ENABLED", True, values=values),
            skills_dir=Path(values.get("SKILLS_DIR", "data/skills")),
            toolpacks_enabled=env_bool("TOOLPACKS_ENABLED", True, values=values),
            toolpacks_dir=Path(values.get("TOOLPACKS_DIR", "data/toolpacks")),
            electricity_history_enabled=env_bool(
                "ELECTRICITY_HISTORY_ENABLED", True, values=values
            ),
            electricity_history_retention_days=retention,
        )
