"""HTTP hosting helpers for the standard MCP Streamable HTTP transport."""

from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import hmac
import ipaddress
import json
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from .exports import ExportDownloadError


@dataclass(frozen=True)
class HTTPServerSettings:
    host: str = "127.0.0.1"
    port: int = 8765
    path: str = "/mcp"
    public_base_url: str = ""
    access_token: str = ""
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    log_level: str = "INFO"

    @property
    def loopback(self) -> bool:
        if self.host.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            return False

    @property
    def normalized_path(self) -> str:
        value = "/" + self.path.strip("/")
        return value if value != "//" else "/"

    @property
    def base_url(self) -> str:
        if self.public_base_url:
            return self.public_base_url.rstrip("/")
        if self.loopback:
            host = "127.0.0.1" if self.host.lower() == "localhost" else self.host
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            return f"http://{host}:{self.port}"
        return ""


def _header(scope: dict[str, Any], name: bytes) -> str:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1")
    return ""


def _host_matches(host: str, patterns: tuple[str, ...], bind_host: str) -> bool:
    host_name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if any(fnmatch.fnmatch(host, pattern) or fnmatch.fnmatch(host_name, pattern) for pattern in patterns):
        return True
    try:
        address = ipaddress.ip_address(host_name.strip("[]"))
    except ValueError:
        address = None
    if bind_host in {"0.0.0.0", "::"} and address is not None:
        return address.is_private or address.is_loopback
    return host_name.casefold() == bind_host.casefold()


class HTTPProtectionMiddleware:
    """Apply bearer-token and Host/Origin checks around SDK routes."""

    def __init__(self, app: Any, settings: HTTPServerSettings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        host = _header(scope, b"host")
        origin = _header(scope, b"origin")
        if not host or not _host_matches(host, self.settings.allowed_hosts, self.settings.host):
            await self._error(send, 400, "invalid_host", "Host is not allowed")
            return
        if origin:
            allowed_origin = any(fnmatch.fnmatch(origin, pattern) for pattern in self.settings.allowed_origins)
            if not allowed_origin:
                origin_netloc = urlsplit(origin).netloc
                origin_host_patterns = tuple(
                    urlsplit(pattern).netloc
                    for pattern in self.settings.allowed_origins
                    if "://" in pattern
                )
                allowed_origin = _host_matches(origin_netloc, origin_host_patterns, self.settings.host)
            if not allowed_origin:
                await self._error(send, 403, "invalid_origin", "Origin is not allowed")
                return
        if path != "/healthz" and self.settings.access_token:
            authorization = _header(scope, b"authorization")
            expected = "Bearer " + self.settings.access_token
            if not hmac.compare_digest(authorization, expected):
                await self._error(
                    send,
                    401,
                    "unauthorized",
                    "Bearer token required",
                    headers=[(b"www-authenticate", b"Bearer")],
                )
                return
        await self.app(scope, receive, send)

    @staticmethod
    async def _error(
        send: Any,
        status_code: int,
        code: str,
        message: str,
        headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        body = json.dumps({"error": code, "message": message}).encode("utf-8")
        response_headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
        response_headers.extend(headers or [])
        await send({"type": "http.response.start", "status": status_code, "headers": response_headers})
        await send({"type": "http.response.body", "body": body})


def build_http_app(
    mcp_app: Any,
    settings: HTTPServerSettings,
    export_manager_factory: Callable[[], Any],
) -> Any:
    """Build an ASGI app from FastMCP's standard Streamable HTTP app."""
    from starlette.requests import Request

    mcp_app.settings.host = settings.host
    mcp_app.settings.port = settings.port
    mcp_app.settings.streamable_http_path = settings.normalized_path
    mcp_app.settings.log_level = settings.log_level
    # The outer middleware performs the same checks for MCP and file routes.
    # Keep the SDK transport itself standard while avoiding a second, divergent
    # allow-list for wildcard/private LAN binds.
    from mcp.server.fastmcp.server import TransportSecuritySettings

    if settings.loopback:
        mcp_app.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(settings.allowed_hosts),
            allowed_origins=list(settings.allowed_origins),
        )
    else:
        mcp_app.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        )

    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok", "transport": "streamable-http"})

    async def download(request: Request) -> Response:
        export_id = request.path_params.get("export_id", "")
        try:
            info = export_manager_factory().download_info(export_id)
        except ExportDownloadError as exc:
            return JSONResponse(
                {"error": exc.code, "message": str(exc), "export_id": exc.export_id},
                status_code=exc.status_code,
            )
        media_type = "text/csv; charset=utf-8" if info["format"] == "csv" else "application/json"
        return FileResponse(
            Path(info["path"]),
            media_type=media_type,
            filename=info["filename"],
            headers={"Content-Length": str(info["file_size"])},
        )

    base_app = mcp_app.streamable_http_app()
    base_app.routes.insert(0, Route("/healthz", health, methods=["GET"]))
    base_app.routes.insert(1, Route("/exports/{export_id}/download", download, methods=["GET"]))
    return HTTPProtectionMiddleware(base_app, settings)
