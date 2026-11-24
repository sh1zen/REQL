"""Exercise installation, guided status, and MCP transport boundaries."""
from __future__ import annotations

from contextlib import closing, redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from agents.install import detect_platforms, install_agent_files, uninstall_agent_files
from launcher import Launcher
from mcp import server
from mcp.tools import TOOL_HANDLERS
from memory import __version__
from memory.config import ConfigError, load_effective_config
from memory.domain.exceptions import StorageError
from memory.domain.models import MemoryNode
from reql import MemoryGraph


def rpc(request_id: int, method: str, **params: object) -> dict[str, object]:
    """Create a request accepted by both MCP transports."""
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


class LocalIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.storage = self.root / ".reql" / "memory.reql"

    def test_installer_uses_custom_profile_for_detection_install_and_removal(self) -> None:
        home = self.root / "home"
        profile = self.root / "custom profile"
        hook = profile / "settings.json"
        profile.mkdir()
        hook.write_text('{"userSetting": true}', encoding="utf-8")
        args = [r"C:\Python Location\python.exe", "-m", "memory.cli"]
        settings = {"home_dir": home, "command_dir": self.root / "bin"}
        with (
            patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(profile)}),
            patch("agents.install.shutil.which", return_value=None),
            patch("agents.install.host_platform.system", return_value="Windows"),
            patch("agents.install._launcher_args", return_value=args) as launcher_args,
        ):
            self.assertEqual(detect_platforms(home_dir=home), ("claude",))
            result = install_agent_files(("claude",), **settings)
            launcher_args.assert_called_once_with()
            actions = {row.kind: row.path for row in result.actions}
            self.assertEqual(actions["hook"], hook)
            self.assertEqual(actions["skill"], profile / "skills" / "reql-agent" / "SKILL.md")
            stamp = json.loads(actions["version"].read_text(encoding="utf-8"))
            self.assertEqual(stamp["package_version"], __version__)
            command = subprocess.list2cmdline(args)
            bootstrap = actions["skill"].parent / "references" / "bootstrap.md"
            self.assertIn(command, bootstrap.read_text(encoding="utf-8"))
            self.assertIn(f"{command} %*", actions["command"].read_text(encoding="utf-8"))
            uninstall_agent_files(("claude",), **settings)
            self.assertEqual(json.loads(hook.read_text(encoding="utf-8")), {"userSetting": True})
            self.assertFalse(actions["skill"].exists())

    def test_launcher_status_reads_real_counts_and_reports_storage_errors(self) -> None:
        """The guided menu uses the store's graph counters and reports failed reads."""
        with closing(Launcher(self.storage)) as launcher, patch("launcher._prompt", return_value="0"):
            launcher.graph.store.upsert_node(MemoryNode(id="status-node", type="Concept", label="status"))
            with redirect_stdout(StringIO()) as stdout:
                self.assertEqual(launcher.run_menu(), 0)
            self.assertIn("1 nodes, 0 edges", stdout.getvalue())
            for error in (PermissionError("access denied"), StorageError("unreadable block")):
                with self.subTest(error=error), patch.object(launcher.graph.store, "count_nodes", side_effect=error):
                    with redirect_stdout(StringIO()), redirect_stderr(StringIO()) as stderr:
                        self.assertEqual(launcher.run_menu(), 0)
                    self.assertIn(f"cannot read storage: {error}", stderr.getvalue())
            with patch.object(launcher.graph.store, "count_nodes", side_effect=TypeError("unexpected signature")):
                with redirect_stdout(StringIO()), self.assertRaisesRegex(TypeError, "unexpected signature"):
                    launcher.run_menu()

    def test_mcp_startup_resolves_file_environment_and_cli_settings(self) -> None:
        config = self.root / "reql.conf"
        config.write_text("mcp:\n  host: 192.0.2.5\n  port: 9000\n  max_depth: 4\n", encoding="utf-8")
        environment = {
            "REQL_MCP_API_KEY": "test-placeholder",
            "REQL_CONFIG_OVERRIDES": '{"mcp.max_depth":2,"mcp":{"port":9001,"max_depth":3}}',
        }
        with patch.dict(os.environ, environment, clear=True), patch("mcp.server.serve_http", return_value=0) as serve:
            self.assertEqual(server.main(["--transport", "http", "--config", str(config),
                                          "--port", "0", "--set", "mcp.max_depth=1", "--read-only"]), 0)
        self.assertEqual(serve.call_args.args, ("192.0.2.5", 0))
        self.assertEqual(serve.call_args.kwargs["limits"].max_depth, 1)
        self.assertFalse(serve.call_args.kwargs["include_write"])
        for key, value in (("port", -1), ("host", "invalid hostname"), ("max_depth", 6), ("max_text_chars", 79)):
            with self.subTest(key=key), self.assertRaisesRegex(ConfigError, f"mcp.{key}"):
                load_effective_config(overrides={f"mcp.{key}": value}, start_dir=self.root, env={})

    def test_mcp_advertises_and_applies_the_same_server_limits(self) -> None:
        config = self.root / "reql.conf"
        config.write_text(
            "mcp:\n  max_top_k: 2\n  max_depth: 1\n  max_items: 3\n  max_nodes: 2\n"
            "  max_edges: 1\n  max_query_rows: 1\n  max_text_chars: 100\n", encoding="utf-8",
        )
        (self.root / "source.py").write_text("def first(): pass\ndef second(): first()\n", encoding="utf-8")
        with closing(MemoryGraph.open(self.storage)) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
        target = {"storage_path": str(self.storage)}
        requests = [
            rpc(1, "tools/list"),
            rpc(2, "tools/call", name="query_graph", arguments={
                **target, "query": "first", "max_nodes": 50, "max_edges": 50,
                "max_depth": 3, "config_overrides": {"mcp.max_nodes": 50},
            }),
            rpc(3, "tools/call", name="query_memories", arguments={**target, "query": "first"}),
            rpc(4, "tools/call", name="reql_query", arguments={
                **target, "statement": "FIND nodes TYPE Function RETURN id", "limit": 20,
            }),
            rpc(5, "tools/call", name="reql_query", arguments={**target, "statement": "UNKNOWN"}),
        ]
        stdin = StringIO("\n".join(json.dumps(request) for request in requests))
        with (
            patch.dict(os.environ, {"REQL_MCP_ALLOWED_ROOTS": str(self.root)}),
            patch("mcp.server.sys.stdin", stdin),
            redirect_stdout(StringIO()) as stdout,
            patch.object(MemoryGraph, "query_graph", return_value={}) as query_graph,
            patch.object(MemoryGraph, "query_memories_payload", return_value={}) as query_memories,
        ):
            self.assertEqual(server.main(["--config", str(config), "--read-only"]), 0)
        frames = [json.loads(line) for line in stdout.getvalue().splitlines()]
        listed, _, _, rows = [frame["result"] for frame in frames[:4]]
        self.assertEqual(frames[4]["error"]["code"], -32602)
        self.assertIn("Expected command", frames[4]["error"]["message"])
        tools = {row["name"]: row["inputSchema"]["properties"] for row in listed["tools"]}
        self.assertEqual(tools["query_context"]["top_k"]["default"], 2)
        self.assertEqual(tools["query_context"]["max_depth"]["maximum"], 1)
        self.assertEqual(tools["query_context"]["max_items"]["maximum"], 3)
        self.assertEqual(tools["query_graph"]["max_nodes"]["maximum"], 2)
        self.assertEqual(tools["query_graph"]["max_edges"]["maximum"], 1)
        self.assertEqual(tools["query_memories"]["max_text_chars"]["maximum"], 100)
        self.assertEqual(tools["reql_query"]["limit"]["maximum"], 1)
        self.assertEqual(query_graph.call_args.kwargs, {
            "top_k": 2, "max_depth": 1, "max_nodes": 2, "max_edges": 1,
            "max_sources": 3, "max_items": 3, "filter_generic": True, "include_archived": False,
        })
        self.assertEqual(query_memories.call_args.kwargs["max_text_chars"], 100)
        self.assertEqual(query_memories.call_args.kwargs["limit"], 3)
        self.assertEqual(len(json.loads(rows["content"][0]["text"])["rows"]), 1)

    def test_both_mcp_transports_negotiate_versions_and_distinguish_errors(self) -> None:
        """Exercise the same frames over stdio and the HTTP handler without opening a socket."""
        requests = [
            "{",
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": ["ping"]}),
            json.dumps(rpc(2, "initialize", protocolVersion="2025-06-18")),
            json.dumps(rpc(3, "initialize", protocolVersion="2099-01-01")),
            json.dumps(rpc(4, "tools/call", name="query_context", arguments=[])),
            json.dumps(rpc(5, "tools/call", name="query_context", arguments={"query": "first"})),
            json.dumps(rpc(6, "ping")),
        ]
        bad_handler = Mock(side_effect=TypeError("unexpected handler defect"))
        with patch.dict(TOOL_HANDLERS, {"query_context": bad_handler}), self.assertLogs("mcp.server", level="ERROR"):
            # This mock has a variadic signature, so the TypeError originates during execution.
            stdio = StringIO()
            self.assertEqual(server.serve(StringIO("\n".join(requests)), stdio, include_write=False), 0)
            frames = [json.loads(line) for line in stdio.getvalue().splitlines()]
            with patch("mcp.server.ThreadingHTTPServer") as create:
                server.create_http_server("127.0.0.1", 0, api_key="test-placeholder", include_write=False)
            handler_class = create.call_args.args[1]
            for request, frame in zip(requests, frames):
                handler = handler_class.__new__(handler_class)
                handler.path = "/mcp"
                body = request.encode("utf-8")
                handler.headers = {
                    "Authorization": "Bearer test-placeholder",
                    "Content-Length": str(len(body)),
                }
                handler.rfile = BytesIO(body)
                handler._send_json = Mock()
                handler.do_POST()
                handler._send_json.assert_called_once_with(200, frame)
        self.assertEqual([frame.get("error", {}).get("code") for frame in frames],
                         [-32700, -32600, None, None, -32602, -32603, None])
        self.assertEqual(frames[2]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(frames[3]["result"]["protocolVersion"], server.PROTOCOL_VERSION)
        self.assertEqual(frames[3]["result"]["serverInfo"]["version"], __version__)
        self.assertEqual(frames[5], {"jsonrpc": "2.0", "id": 5,
                                     "error": {"code": -32603, "message": "Internal error"}})
        handler.headers["MCP-Protocol-Version"] = "2099-01-01"
        handler._send_json.reset_mock()
        handler.do_POST()
        self.assertEqual(handler._send_json.call_args.args[0], 400)
        handler._send_json.reset_mock()
        handler.do_GET()
        self.assertEqual(handler._send_json.call_args.args[0], 405)

        notification = b'{"jsonrpc":"2.0","method":"notifications/initialized"}'
        handler.headers["MCP-Protocol-Version"] = "2025-06-18"
        handler.headers["Content-Length"] = str(len(notification))
        handler.rfile = BytesIO(notification)
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler._send_json.reset_mock()
        handler.do_POST()
        handler.send_response.assert_called_once_with(202)
        handler.send_header.assert_called_once_with("Content-Length", "0")
        handler._send_json.assert_not_called()


if __name__ == "__main__":
    unittest.main()
