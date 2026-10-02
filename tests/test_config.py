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
