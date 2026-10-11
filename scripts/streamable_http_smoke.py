"""Run a real MCP SDK client against a CarbonLens Streamable HTTP endpoint."""

from __future__ import annotations

import argparse
import asyncio
import json
import os

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def smoke(url: str, token: str = "") -> dict[str, object]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(headers=headers) as http_client:
        async with streamable_http_client(url, http_client=http_client, terminate_on_close=False) as (
            read_stream,
            write_stream,
            _,
        ):
            async with ClientSession(read_stream, write_stream) as client:
                initialized = await client.initialize()
                tools = await client.list_tools()
                connections = await client.call_tool("carbon_list_connections", {})
                return {
                    "server": initialized.serverInfo.name,
                    "protocol_version": initialized.protocolVersion,
                    "tool_count": len(tools.tools),
                    "tool_names": sorted(tool.name for tool in tools.tools),
                    "connections_error": bool(connections.isError),
                    "connection_item_count": len(connections.content),
                }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("CARBON_LENS_MCP_URL", "http://127.0.0.1:8765/mcp"))
    parser.add_argument("--token-env", default="CARBON_LENS_HTTP_ACCESS_TOKEN")
    args = parser.parse_args()
    result = asyncio.run(smoke(args.url, os.environ.get(args.token_env, "")))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
