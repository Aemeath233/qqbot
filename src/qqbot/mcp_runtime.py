"""机器人管理stdio MCP子进程，代码不在Webhook进程中执行。"""

import asyncio
import json
import os
import shutil
import sys
from contextlib import suppress
from datetime import timedelta
from importlib.metadata import version
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from qqbot.toolpacks import TOOL_NAME, ToolPackError, ToolPackStore
from qqbot.tools import FunctionTool, validate_schema


def quiet_log():
    # stderr丢到系统空设备，避免插件日志意外包含密钥。
    return open(os.devnull, "w", encoding="utf-8")


class MCPWorker:
    def __init__(self, store, pack, state, *, timeout=15):
        self.store, self.pack, self.config = store, pack, state
        self.timeout = timeout
        self.status, self.message = "starting", "正在启动MCP工具进程。"
        self.schemas = []
        self.queue = asyncio.Queue(maxsize=1)
        self.ready = asyncio.Event()
        self.lock = asyncio.Lock()
        self.task = None
        self.revision = store.revision(pack.name, state)

    def parameters(self):
        entrypoint = self.store.file(self.pack.name, self.pack.entrypoint)
        if self.pack.mode == "functions":
            args = [
                str(Path(__file__).with_name("toolhost.py")),
                str(entrypoint),
                self.pack.name,
                *self.pack.exports,
            ]
        else:
            args = [str(entrypoint)]
        command = sys.executable
        if self.pack.requirements:
            command = shutil.which("uv")
            if not command:
                raise ToolPackError("没有找到uv，请把uv加入服务用户的PATH。")
            args = [
                "run",
                "--no-project",
                "--isolated",
                "--with",
                f"mcp=={version('mcp')}",
                *[part for item in self.pack.requirements for part in ("--with", item)],
                "python",
                *args,
            ]
        env = {
            key: value
            for key, value in self.config.get("env", {}).items()
            if key in self.pack.env_keys and isinstance(value, str)
        }
        env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
        return StdioServerParameters(
            command=command, args=args, cwd=self.store.root / self.pack.name, env=env
        )

    def start(self):
        self.task = asyncio.create_task(self.run(), name=f"mcp-{self.pack.name}")

    async def run(self):
        current = None
        try:
            with quiet_log() as errlog:
                async with stdio_client(self.parameters(), errlog=errlog) as (reader, writer):
                    async with ClientSession(
                        reader, writer, read_timeout_seconds=timedelta(seconds=60)
                    ) as session:
                        async with asyncio.timeout(60):
                            await session.initialize()
                            response = await session.list_tools()
                        if response.nextCursor or len(response.tools) > 8 or not response.tools:
                            raise ToolPackError("每包需提供1～8个工具，不支持工具目录分页。")
                        names = set()
                        for tool in response.tools:
                            if not TOOL_NAME.fullmatch(tool.name) or tool.name in names:
                                raise ToolPackError(
                                    "工具名需为32字以内的英文字母、数字、下划线或连字符。"
                                )
                            parameters = {**tool.inputSchema, "additionalProperties": False}
                            validate_schema(parameters)
                            names.add(tool.name)
                            self.schemas.append(
                                {
                                    "name": tool.name,
                                    "description": (tool.description or tool.name)[:1000],
                                    "parameters": parameters,
                                }
                            )
                        if self.pack.exports and names != set(self.pack.exports):
                            raise ToolPackError("实际工具列表与exports不一致。")
                        self.status, self.message = "running", "MCP工具已就绪。"
                        self.ready.set()
                        while True:
                            name, arguments, current = await self.queue.get()
                            if current.cancelled():
                                current = None
                                continue
                            result = await session.call_tool(
                                name,
                                arguments,
                                read_timeout_seconds=timedelta(seconds=self.timeout),
                            )
                            content = self.result(result)
                            if not current.done():
                                current.set_result(content)
                            current = None
        except asyncio.CancelledError:
            if self.status != "failed":
                self.status, self.message = "stopped", "工具进程已停止。"
            raise
        except Exception as exc:
            self.status = "failed"
            self.message = (
                str(exc)
                if isinstance(exc, ToolPackError)
                else "工具启动或调用失败；请检查源代码、依赖和返回格式。"
            )
        finally:
            self.ready.set()
            error = {"ok": False, "message": self.message}
            if current is not None and not current.done():
                current.set_result(error)
            while not self.queue.empty():
                _, _, pending = self.queue.get_nowait()
                if not pending.done():
                    pending.set_result(error)

    def result(self, result):
        blocks = [
            {"type": "text", "text": block.text[:8000]}
            for block in result.content[:8]
            if block.type == "text"
        ]
        data = {"ok": not result.isError, "content": blocks}
        if result.structuredContent is not None:
            data["data"] = result.structuredContent
        elif len(blocks) == 1:
            with suppress(ValueError, TypeError):
                data["data"] = json.loads(blocks[0]["text"])
        text = json.dumps(data, ensure_ascii=False)
        if len(text) > 16000:
            return {"ok": False, "message": "工具返回超过16000字，请让工具缩小结果。"}
        secrets = [
            value
            for value in self.config.get("env", {}).values()
            if isinstance(value, str) and len(value) >= 4
        ]

        def redact(value):
            if isinstance(value, str):
                for secret in secrets:
                    value = value.replace(secret, "[REDACTED]")
                    value = value.replace(
                        json.dumps(secret, ensure_ascii=False)[1:-1], "[REDACTED]"
                    )
                return value
            if isinstance(value, dict):
                return {redact(key): redact(item) for key, item in value.items()}
            if isinstance(value, list):
                return [redact(item) for item in value]
            return "[REDACTED]" if str(value) in secrets else value

        return redact(data)

    async def call(self, name, arguments):
        async with self.lock:
            if self.status != "running":
                return {"ok": False, "message": self.message}
            future = asyncio.get_running_loop().create_future()
            await self.queue.put((name, arguments, future))
            try:
                return await asyncio.wait_for(future, timeout=self.timeout + 2)
            except TimeoutError:
                self.status, self.message = (
                    "failed",
                    "工具调用超时，已停止进程；未自动重试，请管理员检查后重启。",
                )
                await self.stop()
                return {"ok": False, "message": self.message}
            except asyncio.CancelledError:
                self.status, self.message = (
                    "failed",
                    "调用被取消，已停止工具进程；结果未知，未自动重试。",
                )
                await self.stop()
                raise

    async def stop(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task


class MCPManager:
    def __init__(self, root, *, enabled=True, timeout=15):
        self.store = ToolPackStore(root)
        self.enabled = enabled
        self.timeout = timeout
        self.workers = {}
        self.lock = asyncio.Lock()
        self.monitor = None
        self.closed = False
        self.message = ""

    def active(self):
        return False if self.closed else self.enabled() if callable(self.enabled) else self.enabled

    async def start(self):
        if not self.monitor and not self.closed:
            await self.reconcile()
            self.monitor = asyncio.create_task(self.watch(), name="mcp-manager")

    async def watch(self):
        while True:
            await asyncio.sleep(2)
            await self.reconcile()

    async def reconcile(self):
        async with self.lock:
            try:
                registry = self.store.registry() if self.active() else {}
                enabled = {
                    k: v
                    for k, v in registry.items()
                    if v.get("enabled") is True and v.get("trusted") is True
                }
                if len(enabled) > 4:
                    raise ToolPackError("启用工具包数量超过限制。")
                desired = {
                    name: (self.store.manifest(name), state, self.store.revision(name, state))
                    for name, state in enabled.items()
                }
                self.message = ""
            except (ToolPackError, OSError, ValueError, TypeError):
                self.message = "工具配置无法读取，已停止工具进程，请管理员检查。"
                desired = {}
            for name, worker in list(self.workers.items()):
                if name not in desired or worker.revision != desired[name][2]:
                    await worker.stop()
                    del self.workers[name]
            for name, (pack, state, _) in desired.items():
                if name not in self.workers:
                    worker = MCPWorker(self.store, pack, state, timeout=self.timeout)
                    self.workers[name] = worker
                    worker.start()

    async def restart(self, name):
        async with self.lock:
            worker = self.workers.pop(name, None)
            if worker:
                await worker.stop()
        await self.reconcile()

    async def tools(self):
        await self.reconcile()
        if self.workers:
            await self.start()
        # 首次本地启动留一点等待时间，依赖安装较慢时由管理页显示启动状态。
        if any(worker.status == "starting" for worker in self.workers.values()):
            with suppress(TimeoutError):
                async with asyncio.timeout(5):
                    await asyncio.gather(*(worker.ready.wait() for worker in self.workers.values()))
        tools = []
        for package, worker in list(self.workers.items()):
            if worker.status != "running":
                continue
            for schema in worker.schemas:
                tools.append(
                    FunctionTool(
                        f"mcp__{package}__{schema['name']}",
                        f"{worker.pack.description}：{schema['description']}",
                        schema["parameters"],
                        self.handler(worker, schema["name"]),
                        full_schema=True,
                    )
                )
        return tools

    def handler(self, worker, name):
        results = {}
        call_lock = asyncio.Lock()

        async def execute(**arguments):
            try:
                state = self.store.registry().get(worker.pack.name, {})
                if (
                    not self.active()
                    or state.get("enabled") is not True
                    or state.get("trusted") is not True
                    or worker.revision != self.store.revision(worker.pack.name, state)
                ):
                    return {"ok": False, "message": "工具已停用或配置变化，请重新发起请求。"}
                key = json.dumps(arguments, sort_keys=True, ensure_ascii=False, allow_nan=False)
                async with call_lock:
                    if key in results:
                        return {**results[key], "reused_for_request": True}
                    results[key] = await worker.call(name, arguments)
                    return results[key]
            except (ToolPackError, OSError, ValueError, TypeError):
                return {"ok": False, "message": "工具配置暂时无法读取。"}

        return execute

    async def snapshot(self):
        await self.start()
        await self.reconcile()
        items = self.store.list()
        for item in items:
            worker = self.workers.get(item["name"])
            item.update(
                status=worker.status if worker else "disabled",
                message=worker.message if worker else self.message or "工具未运行。",
                tools=[f"mcp__{item['name']}__{schema['name']}" for schema in worker.schemas]
                if worker and worker.status == "running"
                else [],
            )
        return {"packages": items, "enabled": bool(self.active()), "message": self.message}

    async def close(self):
        self.closed = True
        if self.monitor:
            self.monitor.cancel()
            with suppress(asyncio.CancelledError):
                await self.monitor
            self.monitor = None
        async with self.lock:
            for worker in self.workers.values():
                await worker.stop()
            self.workers.clear()
