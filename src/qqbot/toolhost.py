"""受信任函数包的stdio MCP宿主；仅在独立子进程中导入包。"""

import argparse
import importlib.util
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP


class PluginStdout:
    def __init__(self, protocol_stdout):
        self.buffer = protocol_stdout.buffer

    def write(self, text):
        return sys.stderr.write(text)

    def flush(self):
        return sys.stderr.flush()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("entrypoint", type=Path)
    parser.add_argument("name")
    parser.add_argument("exports", nargs="+")
    args = parser.parse_args()
    entrypoint = args.entrypoint.resolve()
    sys.path.insert(0, str(entrypoint.parent))
    # 用户print不会污染JSON-RPC；协议输出仍由SDK使用原始stdout。
    protocol_stdout = sys.stdout
    sys.stdout = sys.stderr
    spec = importlib.util.spec_from_file_location("uploaded_tools", entrypoint)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load tool entrypoint")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    server = FastMCP(args.name)
    for name in args.exports:
        function = getattr(module, name, None)
        if not callable(function) or not getattr(function, "__doc__", None):
            raise RuntimeError("Exported tools need a callable and docstring")
        server.add_tool(function, name=name)
    sys.stdout = PluginStdout(protocol_stdout)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
