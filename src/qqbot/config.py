"""读取 QQ、模型和电费查询所需的 .env 配置。"""

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


class ConfigurationError(ValueError):
    pass


def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
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
    electricity_cooldown_path: Path = Path("data/electricity-test/cooldown.sqlite3")
    electricity_endpoint: str = (
        "https://cloudpaygateway.59wanmei.com/paygateway/smallpaygateway/trade"
    )

    @classmethod
    def load(
        cls,
        *,
        require_qq: bool = True,
        require_llm: bool = True,
    ) -> "Settings":
        load_dotenv(Path.cwd() / ".env", override=False, encoding="utf-8-sig", interpolate=False)
        app_id = os.getenv("QQ_APP_ID", "").strip()
        app_secret = os.getenv("QQ_APP_SECRET", "").strip()
        if require_qq and (not app_id or not app_secret):
            raise ConfigurationError("请在 .env 中填写 QQ_APP_ID 和 QQ_APP_SECRET。")

        try:
            port = int(os.getenv("QQ_PORT", "8080"))
            pay_project = int(os.getenv("ELECTRICITY_PAY_PROJECT", "953"))
            llm_timeout = float(os.getenv("LLM_TIMEOUT", "30"))
        except ValueError:
            raise ConfigurationError(
                "QQ_PORT、ELECTRICITY_PAY_PROJECT 或 LLM_TIMEOUT 配置无效。"
            ) from None
        if not 1 <= port <= 65535:
            raise ConfigurationError("QQ_PORT 必须为 1～65535 之间的端口。")
        if pay_project <= 0:
            raise ConfigurationError("ELECTRICITY_PAY_PROJECT 必须为正整数。")
        if not math.isfinite(llm_timeout) or not 1 <= llm_timeout <= 120:
            raise ConfigurationError("LLM_TIMEOUT 必须在 1～120 秒之间。")

        log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("LOG_LEVEL 配置无效。")
        llm_enabled = _boolean("LLM_ENABLED", False)
        llm_base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
        llm_api_key = os.getenv("LLM_API_KEY", "").strip()
        llm_model = os.getenv("LLM_MODEL", "").strip()
        if llm_enabled and require_llm and (not llm_api_key or not llm_model):
            raise ConfigurationError("自然语言查询需要填写 LLM_API_KEY 和 LLM_MODEL。")
        llm_enabled = llm_enabled and bool(llm_api_key and llm_model)
        llm_url = urlsplit(llm_base_url)
        if (
            not llm_url.hostname
            or llm_url.username
            or llm_url.password
            or llm_url.query
            or llm_url.fragment
            or llm_url.scheme not in {"http", "https"}
            or (
                llm_url.scheme == "http"
                and llm_url.hostname not in {"localhost", "127.0.0.1", "::1"}
            )
        ):
            raise ConfigurationError("LLM_BASE_URL 必须是 HTTPS 地址；本机模型可使用 HTTP。")

        electricity_enabled = _boolean("ELECTRICITY_ENABLED", False)
        school_code = os.getenv("ELECTRICITY_SCHOOL_CODE", "1402").strip()
        map_value = os.getenv("ELECTRICITY_MAP_PATH", "").strip()
        endpoint = os.getenv(
            "ELECTRICITY_ENDPOINT",
            "https://cloudpaygateway.59wanmei.com/paygateway/smallpaygateway/trade",
        ).strip()
        endpoint_url = urlsplit(endpoint)
        if (
            not endpoint_url.hostname
            or endpoint_url.scheme != "https"
            or endpoint_url.username
            or endpoint_url.password
            or endpoint_url.query
            or endpoint_url.fragment
        ):
            raise ConfigurationError("ELECTRICITY_ENDPOINT 必须是无凭证和查询参数的 HTTPS 地址。")
        if not school_code.isascii() or not school_code.isdigit() or len(school_code) > 20:
            raise ConfigurationError("ELECTRICITY_SCHOOL_CODE 配置无效。")
        if electricity_enabled and school_code != "1402" and not map_value:
            raise ConfigurationError("其他学校需要配置 ELECTRICITY_MAP_PATH。")

        credentials = [
            os.getenv(name, "").strip()
            for name in ("ELECTRICITY_TOKEN", "ELECTRICITY_COOKIE", "ELECTRICITY_TAPP_ID")
        ]
        if any(len(value) > 16384 or "\n" in value or "\r" in value for value in credentials):
            raise ConfigurationError("电费会话配置不能包含换行或超长请求头。")

        db_path = Path(os.getenv("QQ_DB_PATH", "data/qqbot.sqlite3"))
        return cls(
            app_id=app_id,
            app_secret=app_secret,
            host=os.getenv("QQ_HOST", "127.0.0.1").strip(),
            port=port,
            db_path=db_path,
            accept_group_messages=_boolean("QQ_ACCEPT_GROUP_MESSAGES", False),
            log_level=log_level,
            llm_enabled=llm_enabled,
            llm_base_url=llm_base_url,
            llm_api_key=llm_api_key,
            llm_model=llm_model,
            llm_timeout=llm_timeout,
            electricity_enabled=electricity_enabled,
            electricity_school_code=school_code,
            electricity_pay_project=pay_project,
            electricity_map_path=Path(map_value) if map_value else None,
            electricity_token=credentials[0],
            electricity_cookie=credentials[1],
            electricity_tapp_id=credentials[2],
            electricity_default_area=os.getenv("ELECTRICITY_DEFAULT_AREA", "").strip(),
            electricity_cooldown_path=Path(
                os.getenv("ELECTRICITY_COOLDOWN_PATH", "data/electricity-test/cooldown.sqlite3")
            ),
            electricity_endpoint=endpoint,
        )
