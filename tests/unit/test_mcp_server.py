"""单元测试：Haven MCP Server (JSON-RPC 2.0 stdio server) 实现与契约。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
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
    server = MCPServer(services.runtime)
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
        # 断言"被拒了且说清是哪个参数"，而不是具体措辞——措辞不是契约
        assert "goal" in call_err["result"]["content"][0]["text"]

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


# -- 信任边界：不可信的 stdio 客户端不能杀死 server ---------------------------


async def _serve(server: MCPServer, payload: bytes) -> list[dict]:
    """把 payload 喂给 serve_stdio，收回所有响应行。"""
    reader = asyncio.StreamReader()

    class MockWriter:
        def __init__(self) -> None:
            self.lines: list[bytes] = []

        def write(self, data: bytes) -> None:
            self.lines.append(data)

        async def drain(self) -> None:
            pass

    writer = MockWriter()
    reader.feed_data(payload)
    reader.feed_eof()
    await server.serve_stdio(reader, writer)  # type: ignore[arg-type]
    return [json.loads(line) for line in writer.lines]


async def test_non_utf8_bytes_do_not_kill_the_server(tmp_path: Path) -> None:
    """非 UTF-8 的一行曾让 UnicodeDecodeError 逃出 serve_stdio，掀翻整个 server。"""
    server, services = await _create_test_server(tmp_path)
    try:
        responses = await _serve(server, b"\xff\xfe\xff\n" + b'{"id": 1, "method": "ping"}\n')
        # 一个 parse error，一个正常应答——连接活着
        assert responses[0]["error"]["code"] == -32700
        assert responses[-1]["id"] == 1
        assert responses[-1]["result"] == {}
    finally:
        await services.close()


async def test_an_oversized_line_does_not_kill_the_server(tmp_path: Path) -> None:
    """超过 asyncio 流 64 KiB 上限的一行曾让 ValueError 逃出 serve_stdio。

    Haven 自己的 MCP 客户端防住了这一条（见 application/mcp_client.py），
    server 不该比它脆。
    """
    server, services = await _create_test_server(tmp_path)
    try:
        responses = await _serve(server, b"x" * 200_000 + b"\n" + b'{"id": 2, "method": "ping"}\n')
        assert responses[0]["error"]["code"] == -32600
        assert "size limit" in responses[0]["error"]["message"]
        assert responses[-1]["id"] == 2
    finally:
        await services.close()


@pytest.mark.parametrize(
    "payload",
    [
        b'{"id": 3, "method": "ping", "params": "boom"}\n',
        b'{"id": 4, "method": "ping", "params": []}\n',
        b'[{"id": 5, "method": "ping"}]\n',
    ],
)
async def test_malformed_params_do_not_kill_the_server(tmp_path: Path, payload: bytes) -> None:
    """`params` 不是对象、或整个消息是 JSON-RPC 批量数组时，`req.get(...)` 会抛
    AttributeError——那同样曾直接逃出 serve_stdio。"""
    server, services = await _create_test_server(tmp_path)
    try:
        responses = await _serve(server, payload + b'{"id": 6, "method": "ping"}\n')
        assert responses[0]["error"]["code"] == -32600
        assert responses[-1]["id"] == 6
        assert responses[-1]["result"] == {}
    finally:
        await services.close()


# -- tools/list 必须说真话 ----------------------------------------------------


async def test_tools_list_tells_the_truth_under_write(tmp_path: Path) -> None:
    """`--write` 下工具集与只读模式逐字节相同，而描述却声称 "Cannot modify files"。

    外部客户端选工具全靠名称 + inputSchema + description，那是它唯一的信号；
    一句假话等于把整个工作区的写权限交出去，而客户端无从发现真相。
    """
    from haven.adapters.providers.scripted import ScriptedModel

    services = await build_services(
        tmp_path,
        mode=PermissionMode.INTERACTIVE,
        approvals=AutoApprover("approve_all"),
        sinks=[],
        model=ScriptedModel("done."),
        store_path=tmp_path / "haven.db",
    )
    try:
        server = MCPServer(services.runtime, read_only=False)
        resp = await server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tool = next(t for t in resp["result"]["tools"] if t["name"] == "haven.run_readonly")
        assert "Cannot modify files" not in tool["description"]
        assert "--write" in tool["description"]
        # initialize 也要把有效模式报出来，客户端不必从文字里猜
        init = await server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "initialize"})
        assert init["result"]["haven"]["readOnly"] is False
    finally:
        await services.close()

    # 只读模式的描述保持原样
    server_ro, services_ro = await _create_test_server(tmp_path)
    try:
        resp = await server_ro.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tool = next(t for t in resp["result"]["tools"] if t["name"] == "haven.run_readonly")
        assert "Cannot modify files" in tool["description"]
        init = await server_ro.handle_request({"jsonrpc": "2.0", "id": 2, "method": "initialize"})
        assert init["result"]["haven"]["readOnly"] is True
    finally:
        await services_ro.close()


# -- 跨工作区隔离 ------------------------------------------------------------


async def test_resources_do_not_leak_other_workspaces_runs(tmp_path: Path) -> None:
    """会话存储是全局的（所有工作区共用一个库）。不收窄范围的话，
    `resources/list` 会把机器上每个工作区最近 10 次运行的 id 白送出去。"""
    server, services = await _create_test_server(tmp_path)
    try:
        other = "run-from-another-workspace"
        await services.store.create_run(
            other, "/somewhere/else", "f" * 64, "rotate production DB credentials", "interactive"
        )
        listed = await server.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "resources/list"}
        )
        uris = [r["uri"] for r in listed["result"]["resources"]]
        assert not any(other in uri for uri in uris), "别的工作区的运行不该被列出来"

        # 直接按 id 读也要被挡掉
        read = await server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/read",
                "params": {"uri": f"haven://runs/{other}"},
            }
        )
        assert read["error"]["code"] == -32602
        assert "not found" in read["error"]["message"].lower()
    finally:
        await services.close()


# -- schema 边界与异常回显 ----------------------------------------------------


async def test_declared_schema_bounds_are_enforced(tmp_path: Path) -> None:
    """tools/list 声明了 minLength/maxLength，处理器却只查真值。"""
    server, services = await _create_test_server(tmp_path)
    try:
        for bad in ("x", "y" * 5000):
            resp = await server.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "haven.run_readonly", "arguments": {"goal": bad}},
                }
            )
            assert resp["result"]["isError"] is True
            assert "goal" in resp["result"]["content"][0]["text"]

        # skill_names 必须是字符串数组：一个字符串会被当成三个单字符技能名
        resp = await server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "haven.run_readonly",
                    "arguments": {"goal": "a real goal", "skill_names": "abc"},
                },
            }
        )
        assert resp["result"]["isError"] is True
        assert "skill_names" in resp["result"]["content"][0]["text"]
    finally:
        await services.close()


async def test_errors_do_not_echo_the_exception_payload(tmp_path: Path) -> None:
    """catch-all 原先把异常原文反射给客户端——那会把本地技能目录、环境变量名、
    sqlite 路径和绝对路径一起送出去。`feedback.py` 的既定做法是不回显。"""
    server, services = await _create_test_server(tmp_path)
    try:
        resp = await server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "haven.run_readonly",
                    "arguments": {"goal": "unknown skill" * 3, "skill_names": ["nope"]},
                },
            }
        )
        assert resp["result"]["isError"] is True
        text = resp["result"]["content"][0]["text"]
        assert "available:" not in text, "不得泄露本地技能目录"
        assert "Execution failed" in text
    finally:
        await services.close()


# -- diff 资源不再是空桩 -----------------------------------------------------


async def test_the_diff_resource_returns_a_real_diff(tmp_path: Path) -> None:
    """`haven://runs/{id}/diff` 曾恒返回空串。

    `repo.diff` 是只读工具，只读运行（这里的常态）照样会产生差异；客户端无法区分
    "这次运行没改任何东西"和"这个资源没实现"。
    """
    from haven.contracts.events import DiffPreview

    server, services = await _create_test_server(tmp_path)
    try:
        run_id = "run-with-a-diff"
        # 用服务器自己的工作区摘要：存储是全局的，别的摘要会被归属检查挡掉
        await services.store.create_run(
            run_id, str(tmp_path), server._workspace_digest, "change the parser", "interactive"
        )
        await services.store.append_event(
            run_id,
            DiffPreview(
                run_id=run_id,
                files_changed=1,
                insertions=1,
                deletions=1,
                preview="--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y\n",
            ),
        )

        resp = await server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/read",
                "params": {"uri": f"haven://runs/{run_id}/diff"},
            }
        )
        text = resp["result"]["contents"][0]["text"]
        # 拿到的必须是那次运行记录下来的预览本身，而不是空串
        assert text == "--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y\n"
        assert text != ""
    finally:
        await services.close()


# -- 运行期间 server 仍然应答 -------------------------------------------------


class _BlockingModel:
    """第一次请求发一个 delta 然后挂住，用来占住一次运行。"""

    model_name = "blocking"

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def generate_stream(self, request):  # type: ignore[no-untyped-def]

        yield TextDelta(text="starting")
        await self.release.wait()
        yield StreamFinished(finish_reason="stop")
        if False:  # pragma: no cover - 让这个函数保持异步生成器形态
            yield None


async def test_the_server_answers_ping_while_a_run_is_in_flight(tmp_path: Path) -> None:
    """请求曾被串行处理：一次运行会把 `serve_stdio` 堵死，`ping` 和 `cancel` 全都躺在
    StreamReader 里没人读——广告里的取消工具因此在唯一可用的传输上是死代码。"""
    from haven.adapters.providers.scripted import ScriptedModel  # noqa: F401  # 形态参照

    model = _BlockingModel()
    services = await build_services(
        tmp_path,
        mode=PermissionMode.READ_ONLY,
        approvals=AutoApprover("reject_all"),
        sinks=[],
        model=model,  # type: ignore[arg-type]
        store_path=tmp_path / "haven.db",
    )
    server = MCPServer(services.runtime)
    reader = asyncio.StreamReader()

    class MockWriter:
        def __init__(self) -> None:
            self.lines: list[dict] = []

        def write(self, data: bytes) -> None:
            self.lines.append(json.loads(data.decode("utf-8")))

        async def drain(self) -> None:
            pass

    writer = MockWriter()
    try:
        # 先发一次运行（它会挂住），紧接着发一个 ping
        reader.feed_data(
            b'{"id": 1, "method": "tools/call", "params": '
            b'{"name": "haven.run_readonly", "arguments": {"goal": "do a thing"}}}\n'
        )
        reader.feed_data(b'{"id": 2, "method": "ping"}\n')

        serve = asyncio.create_task(server.serve_stdio(reader, writer))  # type: ignore[arg-type]
        try:
            # ping 必须在运行还没结束的时候就得到应答
            async with asyncio.timeout(5):
                while not any(line.get("id") == 2 for line in writer.lines):
                    await asyncio.sleep(0.01)
            assert any(line.get("id") == 2 for line in writer.lines)
        finally:
            model.release.set()
            reader.feed_eof()
            await serve
    finally:
        await services.close()
