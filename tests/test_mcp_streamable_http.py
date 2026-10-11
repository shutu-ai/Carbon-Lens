"""Protocol-level tests for the standard MCP Streamable HTTP transport."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import uuid

import pytest

pytest.importorskip("mcp")

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from carbon_lens import server
from carbon_lens.http import HTTPProtectionMiddleware, HTTPServerSettings, build_http_app


class FakeService:
    def list_connections(self):
        return [{"name": "test", "transport": "beeline", "connection_mode": "ssh", "host": "example", "beeline_connection": "Carbon_Query_XDR"}]


class FakeDownloadManager:
    def __init__(self, path: Path):
        self.path = path

    def download_info(self, export_id: str):
        if export_id != "ce_" + "a" * 32:
            from carbon_lens.exports import ExportDownloadError

            raise ExportDownloadError(404, "unknown_export_id", "unknown export_id", export_id)
        return {"path": self.path, "filename": self.path.name, "format": "csv", "file_size": self.path.stat().st_size}


def make_test_dir() -> Path:
    path = Path(__file__).resolve().parents[1] / (".http-test-" + uuid.uuid4().hex)
    path.mkdir()
    return path


def test_http_settings_require_a_token_for_non_loopback_binds(monkeypatch):
    monkeypatch.delenv("CARBON_LENS_HTTP_ACCESS_TOKEN", raising=False)
    args = server._parser().parse_args(["serve", "--host", "0.0.0.0"])
    with pytest.raises(ValueError, match="non-loopback"):
        server._http_settings_from_args(args)


def test_real_mcp_client_can_initialize_list_and_call_over_streamable_http(monkeypatch):
    root = make_test_dir()
    try:
        output = root / "result.csv"
        output.write_text("name,value\na,1\n", encoding="utf-8")
        monkeypatch.setattr(server, "_service", FakeService())
        settings = HTTPServerSettings(
            host="127.0.0.1",
            port=8765,
            path="/mcp",
            allowed_hosts=("127.0.0.1:*",),
            allowed_origins=("http://127.0.0.1:*",),
        )
        guarded = build_http_app(server.app, settings, lambda: FakeDownloadManager(output))
        base_app = guarded.app

        async def exercise():
            # ASGITransport does not drive lifespan itself; run the same SDK
            # session-manager lifespan that uvicorn would run in production.
            async with base_app.router.lifespan_context(base_app):
                transport = httpx.ASGITransport(app=guarded)
                async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as http_client:
                    async with streamable_http_client(
                        "http://127.0.0.1:8765/mcp",
                        http_client=http_client,
                        terminate_on_close=False,
                    ) as (read_stream, write_stream, _):
                        async with ClientSession(read_stream, write_stream) as client:
                            initialized = await client.initialize()
                            tools = await client.list_tools()
                            called = await client.call_tool("carbon_list_connections", {})
                            return initialized, tools, called

        initialized, tools, called = asyncio.run(exercise())
        assert initialized.serverInfo.name == "carbon-lens"
        names = {tool.name for tool in tools.tools}
        assert len(names) == 22
        assert "carbon_query_start" in names
        assert not called.isError
        payloads = [json.loads(item.text) for item in called.content if hasattr(item, "text")]
        assert any(item["name"] == "test" for item in payloads)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_http_download_is_streamed_and_rejects_unknown_export():
    root = make_test_dir()
    try:
        output = root / "result.csv"
        output.write_text("name,value\na,1\n", encoding="utf-8")
        settings = HTTPServerSettings(
            host="127.0.0.1",
            port=8765,
            allowed_hosts=("127.0.0.1:*",),
            allowed_origins=("http://127.0.0.1:*",),
        )
        guarded = build_http_app(server.app, settings, lambda: FakeDownloadManager(output))

        async def exercise():
            transport = httpx.ASGITransport(app=guarded)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
                good = await client.get("/exports/ce_" + "a" * 32 + "/download")
                bad = await client.get("/exports/ce_" + "b" * 32 + "/download")
                return good, bad

        good, bad = asyncio.run(exercise())
        assert good.status_code == 200
        assert good.headers["content-type"].startswith("text/csv")
        assert good.content == output.read_bytes()
        assert bad.status_code == 404
        assert bad.json()["error"] == "unknown_export_id"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_http_token_protects_mcp_but_not_healthz():
    settings = HTTPServerSettings(
        host="0.0.0.0",
        port=8765,
        access_token="test-token",
        allowed_hosts=("127.0.0.1:*",),
        allowed_origins=("http://127.0.0.1:*",),
    )
    async def endpoint(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    guarded = HTTPProtectionMiddleware(endpoint, settings)

    async def exercise():
        transport = httpx.ASGITransport(app=guarded)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
            health = await client.get("/healthz")
            missing = await client.post("/mcp", json={})
            authorized = await client.post(
                "/mcp",
                json={},
                headers={"Authorization": "Bearer test-token"},
            )
            return health, missing, authorized

    health, missing, authorized = asyncio.run(exercise())
    assert health.status_code == 200
    assert missing.status_code == 401
    assert authorized.status_code != 401
