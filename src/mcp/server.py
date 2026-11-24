"""Dependency-free MCP server for REQL.

The server implements the minimal JSON-RPC methods needed by MCP clients:
``initialize``, ``tools/list`` and ``tools/call``. Tool behavior lives in
``mcp.tools`` so the core package and tests do not depend on a transport.
"""
from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, TextIO

from memory import __version__
from memory.config import (
    CONFIG_OVERRIDES_ENV,
    CONFIG_PATH_ENV,
    ConfigError,
    MCPConfig,
    default_config,
    load_effective_config,
    parse_config_override_assignments,
    parse_config_overrides,
)
from .tools import MCPToolError, call_tool, list_tools

SERVER_NAME = "reql-memory"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]
DEFAULT_API_KEY_ENV = "REQL_MCP_API_KEY"
_LOG = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    defaults = default_config().mcp
    parser = argparse.ArgumentParser(description="Run the optional REQL MCP server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default=None,
        help="Transport to use. Defaults to stdio unless HTTP-specific options are provided.",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Expose only read-only tools. Write tools are hidden from tools/list and rejected.",
    )
    parser.add_argument("--host", default=None, help=f"HTTP bind address, for example 127.0.0.1 or 0.0.0.0. Default: {defaults.host}.")
    parser.add_argument("--port", type=int, default=None, help=f"HTTP bind port. Default: {defaults.port}.")
    parser.add_argument("--api-key", default=None, help="HTTP API key. Required for HTTP transport unless provided through --api-key-env.")
    parser.add_argument("--config", default=None, help="Path to a project reql.conf used by MCP tools")
    parser.add_argument(
        "--set",
        dest="config_overrides",
        action="append",
        default=[],
        metavar="SECTION.OPTION=VALUE",
        help="Override a config value for MCP tools; may be repeated",
    )
    parser.add_argument(
        "--api-key-env",
        default=DEFAULT_API_KEY_ENV,
        help=f"Environment variable used for the HTTP API key. Default: {DEFAULT_API_KEY_ENV}.",
    )
    args = parser.parse_args(argv)

    transport = args.transport
    if transport is None:
        transport = "http" if args.host is not None or args.port is not None or args.api_key is not None else "stdio"

    environment = dict(os.environ)
    if args.config:
        environment[CONFIG_PATH_ENV] = args.config
    try:
        if args.config_overrides:
            existing = parse_config_overrides(environment.get(CONFIG_OVERRIDES_ENV, ""))
            for key, value in parse_config_override_assignments(args.config_overrides).items():
                existing.pop(key, None)
                existing[key] = value
            environment[CONFIG_OVERRIDES_ENV] = json.dumps(existing, separators=(",", ":"))
        bind_overrides = {"mcp.host": args.host, "mcp.port": args.port} if transport == "http" else None
        config = load_effective_config(env=environment, overrides=bind_overrides)
    except ConfigError as exc:
        parser.error(str(exc))

    api_key = args.api_key or environment.get(args.api_key_env, "")
    if transport == "http" and not api_key:
        parser.error(f"HTTP transport requires --api-key or a non-empty {args.api_key_env} environment variable")

    # Tool configuration is still resolved per request, including file edits.
    for key in (CONFIG_PATH_ENV, CONFIG_OVERRIDES_ENV):
        if key in environment:
            os.environ[key] = environment[key]
    if transport == "stdio":
        return serve(sys.stdin, sys.stdout, include_write=not args.read_only, limits=config.mcp)
    return serve_http(config.mcp.host, config.mcp.port, api_key=api_key, include_write=not args.read_only, limits=config.mcp)


def serve(
    stdin: TextIO, stdout: TextIO, *, include_write: bool = True, limits: MCPConfig | None = None,
) -> int:
    """Serve one JSON-RPC message per input line."""
    limits = limits or default_config().mcp
    tools = {tool["name"]: tool for tool in list_tools(include_write=include_write, limits=limits)}
    for line in stdin:
        if not line.strip():
            continue
        response = _process_message(line, tools=tools, limits=limits)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            stdout.flush()
    return 0


def serve_http(
    host: str, port: int, *, api_key: str, include_write: bool = True, limits: MCPConfig | None = None,
) -> int:
    """Serve JSON-RPC MCP requests over HTTP until interrupted."""
    server = create_http_server(host, port, api_key=api_key, include_write=include_write, limits=limits)
    bound_host, bound_port = server.server_address[:2]
    print(f"REQL MCP HTTP server listening on http://{bound_host}:{bound_port}/mcp", file=sys.stderr)
    print("HTTP authorization: Authorization: Bearer <api-key>", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0


def create_http_server(
    host: str, port: int, *, api_key: str, include_write: bool = True, limits: MCPConfig | None = None,
) -> ThreadingHTTPServer:
    """Create an HTTP MCP server instance.

    Tests can bind to port 0 and drive the returned server in a background
    thread without shelling out.
    """
    if not isinstance(api_key, str) or not api_key:
        raise ValueError("api_key must be a non-empty string")
    limits = limits or default_config().mcp
    tools = {tool["name"]: tool for tool in list_tools(include_write=include_write, limits=limits)}

    class MCPHTTPHandler(BaseHTTPRequestHandler):
        server_version = f"REQLMCPHTTP/{__version__}"

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler naming
            if self.path in {"/", "/mcp"}:
                self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method_not_allowed"}, {"Allow": "POST"})
                return
            if self.path != "/health":
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "server": SERVER_NAME,
                    "transport": "http",
                    "read_only": not include_write,
                },
            )

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler naming
            if self.path not in {"/", "/mcp"}:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            if not self._authorized():
                self._discard_body()
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"}, {"WWW-Authenticate": "Bearer"})
                return
            version = self.headers.get("MCP-Protocol-Version")
            if version is not None and version not in SUPPORTED_PROTOCOL_VERSIONS:
                self._send_json(HTTPStatus.BAD_REQUEST, _error_response(None, -32600, "Unsupported MCP-Protocol-Version"))
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0:
                    raise ValueError
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, _error_response(None, -32600, "Invalid Content-Length"))
                return
            response = _process_message(self.rfile.read(length), tools=tools, limits=limits)
            if response is None:
                self.send_response(HTTPStatus.ACCEPTED)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send_json(HTTPStatus.OK, response)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _authorized(self) -> bool:
            expected = f"Bearer {api_key}"
            provided = self.headers.get("Authorization", "")
            return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))

        def _discard_body(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return
            if length > 0:
                self.rfile.read(length)

        def _send_json(self, status: HTTPStatus, payload: dict[str, Any], headers: dict[str, str] | None = None) -> None:
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            if headers:
                for name, value in headers.items():
                    self.send_header(name, value)
            self.end_headers()
            self.wfile.write(encoded)

    return ThreadingHTTPServer((host, port), MCPHTTPHandler)


def _reject_json_constant(value: str) -> None:
    """Reject Python's non-standard JSON numeric literals."""
    raise ValueError(f"Invalid JSON constant: {value}")


def _process_message(
    message: str | bytes, *, tools: dict[str, dict[str, Any]], limits: MCPConfig,
) -> dict[str, Any] | None:
    """Decode a message separately from dispatch, for both transports."""
    try:
        text = message.decode("utf-8") if isinstance(message, bytes) else message
        request = json.loads(text, parse_constant=_reject_json_constant)
    except (ValueError, RecursionError):
        return _error_response(None, -32700, "Parse error")
    return _handle_request(request, tools=tools, limits=limits)


def _handle_request(
    request: object, *, tools: dict[str, dict[str, Any]], limits: MCPConfig,
) -> dict[str, Any] | None:
    """Validate a JSON-RPC envelope and dispatch a supported MCP method."""
    if not isinstance(request, dict):
        return _error_response(None, -32600, "Request must be a JSON object")
    request_id = request.get("id")
    method = request.get("method")
    if (request.get("jsonrpc") != "2.0" or not isinstance(method, str) or not method
            or request_id is not None and type(request_id) not in (str, int)):
        return _error_response(None, -32600, "Invalid JSON-RPC request")
    if "id" not in request:
        return None
    params = request.get("params", {})
    if not isinstance(params, dict):
        return _error_response(request_id, -32602, "Request params must be an object")

    if method == "initialize":
        requested = params.get("protocolVersion")
        if "protocolVersion" in params and (not isinstance(requested, str) or not requested):
            return _error_response(request_id, -32602, "protocolVersion must be a non-empty string")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
        return _result_response(
            request_id,
            {
                "protocolVersion": version,
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
                "capabilities": {"tools": {"listChanged": False}},
            },
        )
    if method == "tools/list":
        return _result_response(request_id, {"tools": list(tools.values())})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments")
        if not isinstance(name, str) or name not in tools:
            return _error_response(request_id, -32602, f"Tool is not available: {name}")
        try:
            payload = call_tool(name, arguments, limits=limits)
            # Preserve source whitespace within a single JSON-RPC frame.
            text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)
        except MCPToolError as exc:
            return _error_response(request_id, -32602, str(exc))
        except Exception:
            _LOG.exception("MCP tool %s failed", name)
            return _error_response(request_id, -32603, "Internal error")
        return _result_response(request_id, {"content": [{"type": "text", "text": text}], "isError": False})
    if method == "ping":
        return _result_response(request_id, {})
    return _error_response(request_id, -32601, f"Method not found: {method}")


def _result_response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


if __name__ == "__main__":
    raise SystemExit(main())
