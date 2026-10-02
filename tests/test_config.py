import pytest

from qqbot.config import ConfigurationError, Settings


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in (
        "QQ_APP_ID",
        "QQ_APP_SECRET",
        "QQ_HOST",
        "QQ_PORT",
        "QQ_DB_PATH",
        "QQ_ACCEPT_GROUP_MESSAGES",
        "LOG_LEVEL",
        "LLM_ENABLED",
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
        "LLM_TIMEOUT",
        "ELECTRICITY_ENABLED",
        "ELECTRICITY_SCHOOL_CODE",
        "ELECTRICITY_PAY_PROJECT",
        "ELECTRICITY_ENDPOINT",
        "ELECTRICITY_MAP_PATH",
        "ELECTRICITY_DEFAULT_AREA",
        "ELECTRICITY_TOKEN",
        "ELECTRICITY_COOKIE",
        "ELECTRICITY_TAPP_ID",
    ):
        monkeypatch.delenv(name, raising=False)


def test_missing_credentials_have_actionable_error():
    with pytest.raises(ConfigurationError, match="QQ_APP_ID"):
        Settings.load()


def test_dotenv_and_environment_priority(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("QQ_APP_ID=from-file\nQQ_APP_SECRET=secret\n", encoding="utf-8")
    monkeypatch.setenv("QQ_APP_ID", "from-env")
    settings = Settings.load()
    assert settings.app_id == "from-env"
    assert "secret" not in repr(settings)


def test_dry_run_separate_database(monkeypatch):
    monkeypatch.setenv("QQ_DB_PATH", "data/live.sqlite3")
    settings = Settings.load(dry_run=True)
    assert settings.app_id == "local-demo"
    assert settings.db_path.name == "live.dry-run.sqlite3"
    monkeypatch.setenv("QQ_HOST", "0.0.0.0")
    with pytest.raises(ConfigurationError, match="本机"):
        Settings.load(dry_run=True)


@pytest.mark.parametrize(
    "name, value",
    [
        ("QQ_PORT", "oops"),
        ("QQ_PORT", "0"),
        ("QQ_PORT", "65536"),
        ("QQ_ACCEPT_GROUP_MESSAGES", "yes"),
        ("LOG_LEVEL", "invalid"),
    ],
)
def test_invalid_configuration(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError):
        Settings.load(dry_run=True)


def test_llm_requires_key_and_model_but_cli_does_not_require_qq(monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "true")
    with pytest.raises(ConfigurationError, match="LLM_API_KEY"):
        Settings.load(require_qq=False)
    monkeypatch.setenv("LLM_API_KEY", "private-model-key")
    monkeypatch.setenv("LLM_MODEL", "model-with-tools")
    settings = Settings.load(require_qq=False)
    assert settings.llm_enabled
    assert "private-model-key" not in repr(settings)


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.example/v1",
        "https://user:pass@model.example/v1",
        "https://model.example/v1?key=x",
    ],
)
def test_insecure_or_credential_bearing_model_urls_rejected(monkeypatch, url):
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("LLM_API_KEY", "dummy-key")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("LLM_BASE_URL", url)
    with pytest.raises(ConfigurationError, match="LLM_BASE_URL"):
        Settings.load(require_qq=False)


def test_dry_run_disables_paid_model_and_electricity_calls(monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("ELECTRICITY_ENABLED", "true")
    settings = Settings.load(dry_run=True)
    assert not settings.llm_enabled
    assert not settings.electricity_enabled


def test_other_school_requires_custom_directory(monkeypatch):
    monkeypatch.setenv("ELECTRICITY_ENABLED", "true")
    monkeypatch.setenv("ELECTRICITY_SCHOOL_CODE", "other-school")
    with pytest.raises(ConfigurationError, match="宿舍映射"):
        Settings.load(require_qq=False)
