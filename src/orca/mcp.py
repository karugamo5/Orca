"""MCP SDK stdio adapter. Public results contain no management metadata."""
import asyncio
import argparse
import logging
from contextlib import asynccontextmanager
from mcp import types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server
from . import __version__
from .config import ToolRegistry, parse_tools, validate_input
from .errors import OrcaError
from .ipc import request
from .platforms.macos import Paths

SCHEMA = {"type": "object", "properties": {
    "prompt": {"type": "string", "minLength": 1, "description": "ローカル処理へ依頼するテキスト"},
    "max_tokens": {"type": "integer", "minimum": 1, "maximum": 65536}},
    "required": ["prompt"], "additionalProperties": False}


class PublicCatalog:
    """Use the daemon's accepted definitions; discover locally only while stopped."""
    def __init__(self, paths: Paths):
        self.paths = paths
        self.registry = ToolRegistry(paths.tools)
        self.tools = None
        self.lock = asyncio.Lock()

    async def load(self):
        async with self.lock:
            try:
                result = await request(self.paths.socket, "tools")
            except OrcaError as error:
                if error.code != "stopped":
                    raise
                try:
                    self.registry.reload()
                except OrcaError:
                    if self.tools is None:
                        raise
                    return self.tools
                definitions = self.registry.tools
            else:
                if (type(result) is not dict or set(result) != {"tools"} or type(result["tools"]) is not list
                        or any(type(row) is not dict or set(row) != {"id", "description"} for row in result["tools"])):
                    raise OrcaError("internal")
                definitions = parse_tools({"tools": [{**row, "models": ["all"]} for row in result["tools"]]})
                # Remember file presence for discovery after the daemon stops.
                # A broken on-disk edit must not replace its accepted catalog.
                try:
                    self.registry.reload()
                except OrcaError:
                    pass
            self.tools = [types.Tool(name=tool.id, description=tool.description, inputSchema=SCHEMA)
                          for tool in definitions]
            return self.tools


def make_server(paths: Paths):
    catalog = PublicCatalog(paths)
    session = advertised = None

    async def watch():
        nonlocal advertised
        while True:
            await asyncio.sleep(1)
            if session is None:
                continue
            try:
                tools = await catalog.load()
            except OrcaError:
                continue
            if tools != advertised:
                await session.send_tool_list_changed()
                advertised = tools

    @asynccontextmanager
    async def lifespan(server):
        task = asyncio.create_task(watch())
        try:
            yield {}
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    server = Server("Orca", version=__version__, lifespan=lifespan)

    @server.list_tools()
    async def list_tools():
        nonlocal session, advertised
        session = server.request_context.session
        tools = await catalog.load()
        if advertised is None:
            advertised = tools
        return tools

    # Validate locally to avoid SDK error formatting echoing invalid arguments.
    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict):
        try:
            validate_input(arguments)
            if name not in {tool.name for tool in await catalog.load()}:
                raise OrcaError("invalid_input")
            result = await request(paths.socket, "generate", {**arguments, "tool_id": name})
            if type(result) is not dict or not isinstance(result.get("text"), str):
                raise OrcaError("internal")
            return types.CallToolResult(content=[types.TextContent(type="text", text=result["text"])])
        except OrcaError as error:
            text = str(error)
            if type(error.http_status) is int and 400 <= error.http_status <= 599:
                text += f" (HTTP {error.http_status})"
            return types.CallToolResult(isError=True, content=[types.TextContent(type="text", text=text)])
        except asyncio.CancelledError:
            raise
        except Exception:
            return types.CallToolResult(isError=True, content=[types.TextContent(type="text", text=str(OrcaError("internal")))])

    return server


async def serve(paths: Paths):
    # Third-party protocol/HTTP diagnostics can include raw input; Orca has its own allowlisted error log.
    logging.disable(logging.CRITICAL)
    server = make_server(paths)
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options(
            notification_options=NotificationOptions(tools_changed=True)))


if __name__ == "__main__":
    from .cli import resolve_paths

    parser = argparse.ArgumentParser(description="Orca MCP stdio adapter", allow_abbrev=False)
    parser.add_argument("--home")
    parser.add_argument("--runtime-dir")
    try:
        asyncio.run(serve(resolve_paths(parser.parse_args())))
    except KeyboardInterrupt:
        raise SystemExit(130)
