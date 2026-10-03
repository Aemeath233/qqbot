import pytest

from qqbot.config import ConfigurationError, Settings


@pytest.mark.parametrize(
    "url",
    [
        "http://public.example",
        "https://user:secret@public.example",
        "https://public.example/path",
        "https://public.example?token=value",
        "https://public.example:invalid",
        "javascript:alert(1)",
        "https://[invalid",
    ],
)
def test_public_address_must_be_https_root_without_credentials(url):
    with pytest.raises(ConfigurationError):
        Settings.from_values({"PORTAL_ENABLED": "true", "PUBLIC_BASE_URL": url}, require_qq=False)


def test_defaults_and_canonical_hosts():
    assert not Settings.from_values({}, require_qq=False).portal_enabled
    settings = Settings.from_values(
        {
            "PORTAL_ENABLED": "true",
            "PUBLIC_BASE_URL": "https://BOT.Example.com:443/",
            "CURVE_LINK_MINUTES": "30",
        },
        require_qq=False,
    )
    assert (
        settings.public_base_url == "https://bot.example.com" and settings.curve_link_minutes == 30
    )
    assert (
        Settings.from_values(
            {"PUBLIC_BASE_URL": "http://127.0.0.1:18080"}, require_qq=False
        ).public_base_url
        == "http://127.0.0.1:18080"
    )
    for values in (
        {"PORTAL_ENABLED": "true"},
        {"CURVE_LINK_MINUTES": "1440"},
        {"PAGE_VISIBILITY": "anything"},
    ):
        with pytest.raises(ConfigurationError):
            Settings.from_values(values, require_qq=False)
