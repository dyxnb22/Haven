"""单元测试：Haven MCP Server (JSON-RPC 2.0 stdio server) 实现与契约。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

from typer.testing import CliRunner

from haven.application.approvals import AutoApprover
from haven.bootstrap import build_services
from haven.contracts.model import ModelEvent, ModelRequest, StreamFinished, TextDelta
from haven.domain.enums import PermissionMode
from haven.interfaces.cli import app
from haven.interfaces.mcp_server import MCPServer

runner = CliRunner()


class ScriptedModel:
    """按预定文本事件流式输出的模型桩。"""

    def __init__(self, text: str = "Analysis completed.") -> None:
        self.text = text

    @property
    def model_name(self) -> str:
        return "deepseek-chat"

    async def _stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        yield TextDelta(text=self.text)
        yield StreamFinished(finish_reason="stop")

    def generate_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        return self._stream(request)

    async def aclose(self) -> None:
        pass


async def _create_test_server(tmp_path: Path) -> tuple[MCPServer, Any]:
    model = ScriptedModel("Inspect success.")
    services = await build_services(
        tmp_path,
        mode=PermissionMode.READ_ONLY,
        approvals=AutoApprover("reject_all"),
        sinks=[],
        model=model,
        store_path=tmp_path / "haven.db",
    )
    server = MCPServer(services.runtime, tmp_path)
    return server, services


async def test_mcp_server_initialize_and_ping(tmp_path: Path) -> None:
    server, services = await _create_test_server(tmp_path)
    try:
        # initialize
        req = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        resp = await server.handle_request(req)
        assert resp is not None
        assert resp["id"] == 1
        assert resp["result"]["protocolVersion"] == "2024-11-05"
        assert resp["result"]["serverInfo"]["name"] == "haven"
        assert "tools" in resp["result"]["capabilities"]
        assert "resources" in resp["result"]["capabilities"]

        # ping
        ping_resp = await server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        assert ping_resp == {"jsonrpc": "2.0", "id": 2, "result": {}}

        # unknown method
        unknown_resp = await server.handle_request(
            {"jsonrpc": "2.0", "id": 3, "method": "non_existent"}
        )
        assert unknown_resp is not None
        assert unknown_resp["error"]["code"] == -32601

        # notification without id returns None
        notif_resp = await server.handle_request({"jsonrpc": "2.0", "method": "ping"})
        assert notif_resp is None
    finally:
        await services.close()


async def test_mcp_server_tools_list_and_call(tmp_path: Path) -> None:
    server, services = await _create_test_server(tmp_path)
    try:
        # list tools
        list_resp = await server.handle_request(
            {"jsonrpc": "2.0", "id": 10, "method": "tools/list"}
        )
        assert list_resp is not None
        tools = list_resp["result"]["tools"]
        tool_names = [t["name"] for t in tools]
        assert "haven.run_readonly" in tool_names
        assert "haven.continue_readonly" in tool_names
        assert "haven.cancel_run" in tool_names

        # call run_readonly missing goal
        call_err = await server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": {"name": "haven.run_readonly"},
            }
        )
        assert call_err is not None
        assert call_err["result"]["isError"] is True
        assert "Missing" in call_err["result"]["content"][0]["text"]

        # call run_readonly valid
        call_ok = await server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 12,
                "method": "tools/call",
                "params": {"name": "haven.run_readonly", "arguments": {"goal": "Check workspace"}},
            }
        )
        assert call_ok is not None
        assert call_ok["result"]["isError"] is False
        payload = json.loads(call_ok["result"]["content"][0]["text"])
        run_id = payload["run_id"]
        assert payload["status"] == "succeeded"
        assert payload["final_text"] == "Inspect success."
        assert payload["resource_uri"] == f"haven://runs/{run_id}"

        # call continue_readonly missing params
        cont_err = await server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 13,
                "method": "tools/call",
                "params": {"name": "haven.continue_readonly", "arguments": {"run_id": run_id}},
            }
        )
        assert cont_err is not None
        assert cont_err["result"]["isError"] is True

        # call continue_readonly valid
        cont_ok = await server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 14,
                "method": "tools/call",
                "params": {
                    "name": "haven.continue_readonly",
                    "arguments": {"run_id": run_id, "follow_up": "Check again"},
                },
            }
        )
        assert cont_ok is not None
        assert cont_ok["result"]["isError"] is False
        cont_payload = json.loads(cont_ok["result"]["content"][0]["text"])
        assert cont_payload["status"] == "succeeded"

        # call cancel_run missing param
        cancel_err = await server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 15,
                "method": "tools/call",
                "params": {"name": "haven.cancel_run", "arguments": {}},
            }
        )
        assert cancel_err is not None
        assert cancel_err["result"]["isError"] is True

        # call cancel_run valid
        cancel_ok = await server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 16,
                "method": "tools/call",
                "params": {"name": "haven.cancel_run", "arguments": {"run_id": run_id}},
            }
        )
        assert cancel_ok is not None
        assert cancel_ok["result"]["isError"] is False
        cancel_data = json.loads(cancel_ok["result"]["content"][0]["text"])
        assert cancel_data["run_id"] == run_id

        # call unknown tool
        unknown_tool = await server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 17,
                "method": "tools/call",
                "params": {"name": "other.action", "arguments": {}},
            }
        )
        assert unknown_tool is not None
        assert unknown_tool["error"]["code"] == -32602
    finally:
        await services.close()


async def test_mcp_server_resources_list_and_read(tmp_path: Path) -> None:
    server, services = await _create_test_server(tmp_path)
    try:
        # Start a run to produce records
        outcome = await services.runtime.start_run("Generate resources")
        run_id = outcome.run_id

        # list resources
        res_list = await server.handle_request(
            {"jsonrpc": "2.0", "id": 20, "method": "resources/list"}
        )
        assert res_list is not None
        uris = [r["uri"] for r in res_list["result"]["resources"]]
        assert f"haven://runs/{run_id}" in uris
        assert f"haven://runs/{run_id}/events" in uris
        assert f"haven://runs/{run_id}/diff" in uris
        assert f"haven://runs/{run_id}/evidence" in uris

        # read root overview
        read_root = await server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 21,
                "method": "resources/read",
                "params": {"uri": f"haven://runs/{run_id}"},
            }
        )
        assert read_root is not None
        body = json.loads(read_root["result"]["contents"][0]["text"])
        assert body["run_id"] == run_id
        assert body["goal"] == "Generate resources"

        # read events
        read_events = await server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 22,
                "method": "resources/read",
                "params": {"uri": f"haven://runs/{run_id}/events"},
            }
        )
        assert read_events is not None
        events = json.loads(read_events["result"]["contents"][0]["text"])
        assert isinstance(events, list)
        assert len(events) > 0

        # read evidence
        read_evidence = await server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 23,
                "method": "resources/read",
                "params": {"uri": f"haven://runs/{run_id}/evidence"},
            }
        )
        assert read_evidence is not None
        evidence = json.loads(read_evidence["result"]["contents"][0]["text"])
        assert isinstance(evidence, dict)

        # read diff
        read_diff = await server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 24,
                "method": "resources/read",
                "params": {"uri": f"haven://runs/{run_id}/diff"},
            }
        )
        assert read_diff is not None
        assert read_diff["result"]["contents"][0]["text"] == ""

        # invalid URI
        bad_uri = await server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 25,
                "method": "resources/read",
                "params": {"uri": "file:///etc/passwd"},
            }
        )
        assert bad_uri is not None
        assert bad_uri["error"]["code"] == -32602

        # not found run_id
        missing_run = await server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 26,
                "method": "resources/read",
                "params": {"uri": "haven://runs/run_non_existent"},
            }
        )
        assert missing_run is not None
        assert missing_run["error"]["code"] == -32602

        # unknown subpath
        bad_sub = await server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 27,
                "method": "resources/read",
                "params": {"uri": f"haven://runs/{run_id}/unknown_sub"},
            }
        )
        assert bad_sub is not None
        assert bad_sub["error"]["code"] == -32602
    finally:
        await services.close()


async def test_mcp_server_serve_stdio(tmp_path: Path) -> None:
    server, services = await _create_test_server(tmp_path)
    try:
        reader = asyncio.StreamReader()

        class MockWriter:
            def __init__(self) -> None:
                self.lines: list[str] = []

            def write(self, data: bytes) -> None:
                self.lines.append(data.decode("utf-8"))

            async def drain(self) -> None:
                pass

        writer = MockWriter()

        # Feed invalid JSON, then valid ping, then notification, then empty
        reader.feed_data(b"not valid json\n")
        reader.feed_data(b'{"jsonrpc": "2.0", "id": 99, "method": "ping"}\n')
        reader.feed_data(b'{"jsonrpc": "2.0", "method": "notification"}\n')
        reader.feed_eof()

        await server.serve_stdio(reader, writer)  # type: ignore[arg-type]

        assert len(writer.lines) == 2
        err_obj = json.loads(writer.lines[0])
        assert err_obj["error"]["code"] == -32700
        ping_obj = json.loads(writer.lines[1])
        assert ping_obj["id"] == 99
        assert ping_obj["result"] == {}
    finally:
        await services.close()


def test_cli_mcp_serve_invocation(tmp_path: Path) -> None:
    with patch("haven.interfaces.mcp_server.run_mcp_server") as mock_serve:
        mock_serve.return_value = None
        result = runner.invoke(app, ["mcp", "serve", "--workspace", str(tmp_path)])
        assert result.exit_code == 0
        mock_serve.assert_called_once()
