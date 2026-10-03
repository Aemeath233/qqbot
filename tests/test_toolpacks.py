import asyncio
import io
import json
from dataclasses import replace
from zipfile import ZipFile

import aiohttp
import pytest

from qqbot.assistant import BotAssistant
from qqbot.mcp_runtime import MCPManager, MCPWorker
from qqbot.toolpacks import ToolPackError, ToolPackStore, parse_manifest
from qqbot.tools import FunctionTool, ToolRegistry, validate_schema


def archive(source, *, config=None, extras=None):
    data = {
        "version": 1,
        "name": "demo",
        "description": "Test functions",
        "entrypoint": "tools.py",
        "exports": ["add"],
    }
    data.update(config or {})
    stream = io.BytesIO()
    with ZipFile(stream, "w") as package:
        package.writestr("demo/toolpack.json", json.dumps(data))
        package.writestr("demo/tools.py", source)
        for path, content in (extras or {}).items():
            package.writestr(path, content)
    return stream.getvalue()


SOURCE = '''
def add(a: float, b: float, rounding: bool = False) -> dict:
    """Add two numbers, optionally round the result."""
    print("function log goes to stderr")
    result = a + b
    return {"answer": round(result) if rounding else result}
'''


async def wait_ready(manager, name="demo"):
    await manager.start()
    await asyncio.wait_for(manager.workers[name].ready.wait(), timeout=15)
    return manager.workers[name]


@pytest.mark.parametrize(
    "config",
    [
        {"name": "../escape"},
        {"name": "CON"},
        {"version": True},
        {"unknown": "ignored?"},
        {"entrypoint": "../run.py"},
        {"exports": []},
        {"exports": ["add", "add"]},
        {"requirements": ["https://example.test/package.whl"]},
        {"requirements": ["-rfile.txt"]},
        {"requirements": ["mcp>=2"]},
        {"env_keys": ["LLM_API_KEY"]},
        {"env_keys": ["PYTHONPATH"]},
        {"env_keys": ["PATH"]},
        {"env_keys": ["TOKEN", "TOKEN"]},
        {"mode": "shell"},
    ],
)
def test_manifest_rejects_invalid_names_paths_dependencies_and_parent_env(config):
    base = {"version": 1, "name": "demo", "description": "Test", "exports": ["add"]}
    base.update(config)
    with pytest.raises(ToolPackError):
        parse_manifest(json.dumps(base).encode())


def test_install_never_imports_code_requires_explicit_trust_and_keeps_env_private(tmp_path):
    store = ToolPackStore(tmp_path / "tools")
    sentinel = tmp_path / "executed.txt"
    source = (
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('unexpected')\n" + SOURCE
    )
    item = store.install("demo.zip", archive(source, config={"env_keys": ["DEMO_API_KEY"]}))
    assert not item["enabled"] and not sentinel.exists()
    with pytest.raises(ToolPackError, match="信任"):
        store.enable("demo", True)
    with pytest.raises(ToolPackError, match="同名"):
        store.install("demo.zip", archive(SOURCE))
    store.set_env("demo", {"DEMO_API_KEY": "mock-private-key"}, [])
    public = json.dumps(store.list())
    assert "mock-private-key" not in public and store.list()[0]["env_configured"]["DEMO_API_KEY"]
    store.set_env("demo", {"DEMO_API_KEY": ""}, [])
    assert store.registry()["demo"]["env"]["DEMO_API_KEY"] == "mock-private-key"
    store.set_env("demo", {}, ["DEMO_API_KEY"])
    assert not store.list()[0]["env_configured"]["DEMO_API_KEY"]
    with pytest.raises(ToolPackError):
        store.set_env("demo", {"QQ_APP_SECRET": "wrong"}, [])


@pytest.mark.parametrize(
    "source,extras",
    [("invalid python !!!", {}), (SOURCE, {"outside": "bad"}), (SOURCE, {"demo/../escape": "bad"})],
)
def test_invalid_source_or_archive_never_creates_package(tmp_path, source, extras):
    store = ToolPackStore(tmp_path)
    with pytest.raises(ToolPackError):
        store.install("demo.zip", archive(source, extras=extras))
    assert store.list() == []


async def test_real_stdio_auto_start_typed_call_stop_and_stale_handler(tmp_path):
    manager = MCPManager(tmp_path)
    manager.store.install("demo.zip", archive(SOURCE))
    try:
        await manager.start()
        assert manager.workers == {}
        manager.store.enable("demo", True, trusted=True)
        await manager.reconcile()
        worker = await wait_ready(manager)
        assert worker.status == "running", worker.message
        registry = ToolRegistry()
        tools = await manager.tools()
        assert [tool.name for tool in tools] == ["mcp__demo__add"]
        registry.register(tools[0])
        result = await registry.execute("mcp__demo__add", '{"a":1.25,"b":2.75,"rounding":true}')
        assert result["ok"] and result["data"]["answer"] == 4
        assert not (await registry.execute("mcp__demo__add", '{"a":"wrong","b":2}'))["ok"]
        manager.store.enable("demo", False)
        assert not (await registry.execute("mcp__demo__add", '{"a":1,"b":2}'))["ok"]
        await manager.reconcile()
        assert worker.task.done() and manager.workers == {}
    finally:
        await manager.close()


async def test_worker_env_does_not_inherit_bot_keys_and_redacts_own_key(tmp_path, monkeypatch):
    source = '''
import os
def environment() -> dict:
    """Return diagnostic flags for the test."""
    return {"parent_key": os.environ.get("LLM_API_KEY", "missing"),
            "own_key": os.environ.get("DEMO_API_KEY", "missing")}
'''
    monkeypatch.setenv("LLM_API_KEY", "mock-parent-key")
    manager = MCPManager(tmp_path)
    manager.store.install(
        "demo.zip",
        archive(source, config={"exports": ["environment"], "env_keys": ["DEMO_API_KEY"]}),
    )
    own_secret = 'mock-own-"key\n'
    manager.store.set_env("demo", {"DEMO_API_KEY": own_secret}, [])
    manager.store.enable("demo", True, True)
    try:
        worker = await wait_ready(manager)
        assert worker.status == "running", worker.message
        result = await worker.call("environment", {})
        assert result["data"]["parent_key"] == "missing"
        assert result["data"]["own_key"] == "[REDACTED]"
        assert own_secret not in json.dumps(result)
        assert "mock-own-" not in json.dumps(result)
    finally:
        await manager.close()


async def test_timeout_stops_process_without_automatic_replay(tmp_path):
    source = '''
import time
def slow() -> dict:
    """A deliberately slow test tool."""
    time.sleep(20)
    return {"done": True}
'''
    manager = MCPManager(tmp_path, timeout=0.2)
    manager.store.install("demo.zip", archive(source, config={"exports": ["slow"]}))
    manager.store.enable("demo", True, True)
    try:
        worker = await wait_ready(manager)
        assert worker.status == "running", worker.message
        result = await worker.call("slow", {})
        assert not result["ok"]
        assert worker.task.done() and worker.status == "failed"
        await manager.reconcile()
        assert manager.workers["demo"] is worker
        assert not (await worker.call("slow", {}))["ok"]
    finally:
        await manager.close()


async def test_two_hosts_sync_restart_and_secret_changes(tmp_path):
    first, second = MCPManager(tmp_path), MCPManager(tmp_path)
    first.store.install("demo.zip", archive(SOURCE, config={"env_keys": ["TOKEN"]}))
    first.store.enable("demo", True, True)
    try:
        original_a, original_b = await asyncio.gather(wait_ready(first), wait_ready(second))
        first.store.request_restart("demo")
        await asyncio.gather(first.reconcile(), second.reconcile())
        assert original_a.task.done() and original_b.task.done()
        assert first.workers["demo"] is not original_a and second.workers["demo"] is not original_b
        worker = await wait_ready(first)
        first.store.set_env("demo", {"TOKEN": "mock-new-token"}, [])
        await first.reconcile()
        assert worker.task.done() and first.workers["demo"] is not worker
    finally:
        await asyncio.gather(first.close(), second.close())


async def test_global_off_and_bad_import_never_grants_tools(tmp_path):
    manager = MCPManager(tmp_path, enabled=False)
    manager.store.install(
        "demo.zip", archive("raise RuntimeError('private-trace-text')\n" + SOURCE)
    )
    manager.store.enable("demo", True, True)
    try:
        assert await manager.tools() == [] and manager.workers == {}
        manager.enabled = True
        worker = await wait_ready(manager)
        assert worker.status == "failed" and "private-trace-text" not in worker.message
        assert await manager.tools() == []
    finally:
        await manager.close()


async def test_same_tool_arguments_reuse_result_only_within_one_request(tmp_path):
    source = '''
count = 0
def count_calls(value: int) -> dict:
    """Count actual function executions for the test."""
    global count
    count += 1
    return {"calls": count, "value": value}
'''
    manager = MCPManager(tmp_path)
    manager.store.install("demo.zip", archive(source, config={"exports": ["count_calls"]}))
    manager.store.enable("demo", True, True)
    try:
        await wait_ready(manager)
        first_request = (await manager.tools())[0].handler
        assert (await first_request(value=1))["data"]["calls"] == 1
        repeated = await first_request(value=1)
        assert repeated["data"]["calls"] == 1 and repeated["reused_for_request"]
        second_request = (await manager.tools())[0].handler
        assert (await second_request(value=1))["data"]["calls"] == 2
        manager.store.enable("demo", False)
        assert not (await first_request(value=1))["ok"]
    finally:
        await manager.close()


async def test_existing_python_stdio_server_can_be_managed(tmp_path):
    source = '''
from mcp.server.fastmcp import FastMCP
server = FastMCP("Existing stdio server")
@server.tool()
def echo(text: str) -> dict:
    """Return the user's text."""
    return {"text": text}
server.run(transport="stdio")
'''
    manager = MCPManager(tmp_path)
    manager.store.install("demo.zip", archive(source, config={"mode": "mcp-stdio", "exports": []}))
    manager.store.enable("demo", True, True)
    try:
        worker = await wait_ready(manager)
        assert worker.status == "running", worker.message
        assert (await worker.call("echo", {"text": "你好"}))["data"]["text"] == "你好"
    finally:
        await manager.close()


def test_uv_uses_argument_array_and_declared_dependencies(tmp_path, monkeypatch):
    store = ToolPackStore(tmp_path)
    store.install("demo.zip", archive(SOURCE, config={"requirements": ["humanize>=4,<5"]}))
    monkeypatch.setattr("qqbot.mcp_runtime.shutil.which", lambda name: "/trusted/uv")
    worker = MCPWorker(store, store.manifest("demo"), {"env": {}})
    params = worker.parameters()
    assert params.command == "/trusted/uv" and "humanize>=4,<5" in params.args
    assert "--isolated" in params.args and "--no-project" in params.args
    assert "shell" not in params.args


async def test_json_schema_arrays_nested_refs_and_no_external_resolution():
    schema = {
        "type": "object",
        "properties": {"values": {"type": "array", "items": {"$ref": "#/$defs/number"}}},
        "$defs": {"number": {"type": "number"}},
        "required": ["values"],
        "additionalProperties": False,
    }
    validate_schema(schema)

    async def handler(**kwargs):
        return {"ok": True, "values": kwargs["values"]}

    registry = ToolRegistry()
    registry.register(FunctionTool("nested", "test", schema, handler, full_schema=True))
    assert (await registry.execute("nested", '{"values":[1,2.5]}'))["ok"]
    assert not (await registry.execute("nested", '{"values":["bad"]}'))["ok"]
    for bad in (
        {"$ref": "https://example.test/schema"},
        {"$dynamicRef": "https://example.test/schema"},
        {"$id": "https://example.test/schema"},
    ):
        with pytest.raises(ValueError):
            validate_schema({"type": "object", **bad})


async def test_assistant_calls_uploaded_function_from_model(settings):
    settings = replace(settings, llm_enabled=True)
    manager = MCPManager(settings.toolpacks_dir)
    manager.store.install("demo.zip", archive(SOURCE))
    manager.store.enable("demo", True, True)
    seen = []

    class Model:
        async def complete(self, messages, tools):
            seen.append((messages.copy(), tools))
            if len(seen) == 1:
                return {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "mcp-1",
                            "type": "function",
                            "function": {"name": "mcp__demo__add", "arguments": '{"a":1,"b":2}'},
                        }
                    ],
                }
            result = json.loads(messages[-1]["content"])
            return {"role": "assistant", "content": str(result["data"]["answer"])}

    try:
        await wait_ready(manager)
        async with aiohttp.ClientSession() as session:
            assistant = BotAssistant(settings, session, model=Model(), tool_manager=manager)
            assert await assistant.generate("chat", "帮我把1和2加起来", "person") == "3.0"
        assert any(t["function"]["name"] == "mcp__demo__add" for t in seen[0][1])
        assert seen[1][0][-1]["role"] == "tool"
    finally:
        await manager.close()
