import subprocess
import sys


def test_cli_demo_handles_chinese_in_pipes():
    result = subprocess.run(
        [sys.executable, "-m", "qqbot", "demo"],
        input="/ping\n/复读 你好\nexit\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=5,
    )
    assert result.returncode == 0
    assert "pong" in result.stdout
    assert "机器人：你好" in result.stdout


def test_cli_help_is_readable_chinese():
    result = subprocess.run(
        [sys.executable, "-m", "qqbot", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=5,
    )
    assert result.returncode == 0
    assert "QQ 群聊与私聊机器人" in result.stdout
