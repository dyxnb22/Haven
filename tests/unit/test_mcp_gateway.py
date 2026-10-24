"""单元测试：受控 MCP（Model Context Protocol）网关（ADR 0039）。

覆盖：
1. 配置解析：[mcp.servers.<name>] 字段验证与规范化；
2. MCPClient & JSON-RPC 握手与生命周期（initialize, tools/list, tools/call）；
3. 白名单过滤：只有 allowed_tools 暴露给模型，未允许的工具静默忽略；
4. 命名空间隔离：统一转换为 mcp.<server_name>.<tool_name>；
5. Schema 指纹绑定与漂移防御：指纹不匹配时 fail-closed 判定为 mcp_schema_drift 并拒绝；
6. 确定性策略评估：只读放行、写操作需要审批、只读模式拒绝写操作、漂移强制拒绝；
7. 审批卡片渲染：明确呈现 MCP Server、工具名与调用载荷；
8. 工具注册表与参数校验；
9. 客户端管理器全生命周期与孤儿清理。
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

from haven.application.approval_cards import ApprovalCardRenderer
from haven.application.mcp_client import MCPClient, MCPClientManager, MCPSchemaDriftError
from haven.application.registry import ToolRegistry, ValidationFailure
from haven.config import ConfigError, _parse_mcp_servers
from haven.contracts.mcp import MCPServerConfig, MCPToolArgs, MCPToolSchema
from haven.domain.digest import canonical_json, sha256_text
from haven.domain.enums import PermissionMode, PolicyDecision, RiskLevel
from haven.domain.policy import ToolFacts, evaluate_policy

MOCK_MCP_SERVER_SCRIPT = """
import sys, json

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    method = req.get("method")
    req_id = req.get("id")

    if method == "initialize":
        res = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mock-mcp-server", "version": "1.0.0"}
            }
        }
        sys.stdout.write(json.dumps(res) + "\\n")
        sys.stdout.flush()
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        res = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {
                        "name": "lookup",
                        "description": "Look up doc entries",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                        },
                    },
                    {
                        "name": "execute_mutation",
                        "description": "Mutating operation",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"action": {"type": "string"}},
                        },
                    },
                    {
                        "name": "unauthorized_tool",
                        "description": "Should never be exposed",
                        "inputSchema": {"type": "object", "properties": {}}
                    }
                ]
            }
        }
        sys.stdout.write(json.dumps(res) + "\\n")
        sys.stdout.flush()
    elif method == "tools/call":
        params = req.get("params", {})
        tool_name = params.get("name")
        args = params.get("arguments", {})
        res = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": f"called {tool_name} with {json.dumps(args)}"}]
            }
        }
        sys.stdout.write(json.dumps(res) + "\\n")
        sys.stdout.flush()
"""


def test_config_parsing_mcp_servers() -> None:
    toml_str = """
[mcp.servers.github]
command = "uvx"
args = ["mcp-github", "--verbose"]
env = { GITHUB_TOKEN = "token-123" }
allowed_tools = ["search_issues", "create_issue"]
read_only_tools = ["search_issues"]
pinned_schemas = { search_issues = "sha256-hash-value" }
"""
    parsed = tomllib.loads(toml_str)
    servers = _parse_mcp_servers(parsed.get("mcp"), origin="test.toml")
    assert len(servers) == 1
    srv = servers[0]
    assert srv.name == "github"
    assert srv.command == "uvx"
    assert srv.args == ("mcp-github", "--verbose")
    assert srv.env == {"GITHUB_TOKEN": "token-123"}
    assert srv.allowed_tools == ("search_issues", "create_issue")
    assert srv.read_only_tools == ("search_issues",)
    assert srv.pinned_schemas == {"search_issues": "sha256-hash-value"}


def test_config_parsing_empty_and_invalid() -> None:
    assert _parse_mcp_servers({}, origin="") == ()
    with pytest.raises(ConfigError, match="must be a table"):
        _parse_mcp_servers(None, origin="bad.toml")
    with pytest.raises(ConfigError, match=r"\[mcp\.servers\] in bad\.toml must be a table"):
        _parse_mcp_servers({"servers": "not_a_dict"}, origin="bad.toml")


@pytest.mark.asyncio
async def test_mcp_client_handshake_and_filtering() -> None:
    # 预计算 lookup 的输入 schema 哈希
    lookup_schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    valid_digest = sha256_text(canonical_json(lookup_schema))

    config = MCPServerConfig(
        name="test_srv",
        command=sys.executable,
        args=("-c", MOCK_MCP_SERVER_SCRIPT),
        allowed_tools=("lookup", "execute_mutation"),
        read_only_tools=("lookup",),
        pinned_schemas={"lookup": valid_digest},
    )

    client = MCPClient(config)
    assert not client.is_running
    assert client.last_stderr == ""

    try:
        await client.start()
        assert client.is_running

        tools = await client.discover_tools()
        assert len(tools) == 2

        tool_map = {t.tool_name: t for t in tools}
        assert "lookup" in tool_map
        assert "execute_mutation" in tool_map
        assert "unauthorized_tool" not in tool_map  # 白名单拦截

        lookup_tool = tool_map["lookup"]
        assert lookup_tool.namespaced_name == "mcp.test_srv.lookup"
        assert lookup_tool.is_read_only is True
        assert lookup_tool.schema_digest == valid_digest
        assert not client.is_drifted("mcp.test_srv.lookup")

        mutation_tool = tool_map["execute_mutation"]
        assert mutation_tool.namespaced_name == "mcp.test_srv.execute_mutation"
        assert mutation_tool.is_read_only is False

        # 测试正常调用
        res = await client.call_tool("lookup", {"query": "pytest"})
        assert not res["is_error"]
        assert 'called lookup with {"query": "pytest"}' in res["content"]
    finally:
        await client.close()
        assert not client.is_running


@pytest.mark.asyncio
async def test_mcp_schema_drift_defense() -> None:
    # 使用错误的绑定指纹模拟外部 Server 发生静默变更
    config = MCPServerConfig(
        name="drift_srv",
        command=sys.executable,
        args=("-c", MOCK_MCP_SERVER_SCRIPT),
        allowed_tools=("lookup",),
        read_only_tools=("lookup",),
        pinned_schemas={"lookup": "tampered_expected_hash_value"},
    )

    client = MCPClient(config)
    try:
        await client.start()
        tools = await client.discover_tools()
        assert len(tools) == 1
        assert client.is_drifted("mcp.drift_srv.lookup")

        with pytest.raises(MCPSchemaDriftError, match="schema digest drifted"):
            await client.call_tool("lookup", {"query": "foo"})
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mcp_client_manager_lifecycle() -> None:
    config = MCPServerConfig(
        name="mgr_srv",
        command=sys.executable,
        args=("-c", MOCK_MCP_SERVER_SCRIPT),
        allowed_tools=("lookup", "execute_mutation"),
        read_only_tools=("lookup",),
    )

    manager = MCPClientManager((config,))
    try:
        discovered = await manager.initialize()
        assert len(discovered) == 2
        assert len(manager.tools) == 2

        # 验证幂等性
        re_init = await manager.initialize()
        assert re_init == discovered

        # 验证工具查找
        lookup_schema = manager.get_tool("mcp.mgr_srv.lookup")
        assert lookup_schema is not None
        assert lookup_schema.tool_name == "lookup"

        # 验证调用
        res = await manager.call_tool("mcp.mgr_srv.lookup", {"query": "hello"})
        assert not res["is_error"]
        assert "hello" in res["content"]

        # 验证未知工具调用
        unknown_res = await manager.call_tool("mcp.mgr_srv.not_exist", {})
        assert unknown_res["is_error"]
        assert "unknown MCP tool" in unknown_res["error"]
    finally:
        await manager.close_all()
        assert len(manager.tools) == 0


def test_mcp_policy_evaluation() -> None:
    # 1. Schema 漂移一律强制 DENY (高风险)
    drifted_facts = ToolFacts(
        tool_name="mcp.srv.op",
        mcp_server="srv",
        mcp_read_only=True,
        mcp_schema_drift=True,
    )
    outcome = evaluate_policy(PermissionMode.INTERACTIVE, drifted_facts)
    assert outcome.decision == PolicyDecision.DENY
    assert outcome.reason_code == "mcp_schema_drift"
    assert outcome.risk == RiskLevel.HIGH

    # 2. 只读 MCP 工具自动放行 (低风险)
    read_only_facts = ToolFacts(
        tool_name="mcp.srv.query",
        mcp_server="srv",
        mcp_read_only=True,
        mcp_schema_drift=False,
    )
    outcome_ro_ws = evaluate_policy(PermissionMode.INTERACTIVE, read_only_facts)
    assert outcome_ro_ws.decision == PolicyDecision.ALLOW
    assert outcome_ro_ws.reason_code == "mcp_read_only"
    assert outcome_ro_ws.risk == RiskLevel.LOW

    outcome_ro_ro = evaluate_policy(PermissionMode.READ_ONLY, read_only_facts)
    assert outcome_ro_ro.decision == PolicyDecision.ALLOW
    assert outcome_ro_ro.reason_code == "mcp_read_only"

    # 3. 有副作用 MCP 工具在只读模式下拒绝
    mutating_facts = ToolFacts(
        tool_name="mcp.srv.mutate",
        mcp_server="srv",
        mcp_read_only=False,
        mcp_schema_drift=False,
    )
    outcome_mut_ro = evaluate_policy(PermissionMode.READ_ONLY, mutating_facts)
    assert outcome_mut_ro.decision == PolicyDecision.DENY
    assert outcome_mut_ro.reason_code == "read_only_mode"

    # 4. 有副作用 MCP 工具在写模式下要求审批 (高风险)
    outcome_mut_ws = evaluate_policy(PermissionMode.INTERACTIVE, mutating_facts)
    assert outcome_mut_ws.decision == PolicyDecision.ASK
    assert outcome_mut_ws.reason_code == "mcp_requires_approval"
    assert outcome_mut_ws.risk == RiskLevel.HIGH


def test_mcp_approval_card_rendering() -> None:
    renderer = ApprovalCardRenderer(recipes={}, sandbox_description=lambda: "seatbelt")
    args = MCPToolArgs(arguments={"commit": "HEAD~1", "force": True})
    summary, preview = renderer.render("mcp.git_server.reset_head", args, None)

    assert "call MCP tool 'reset_head' on server 'git_server'" in summary
    assert "MCP Server: git_server" in preview
    assert "Tool: reset_head" in preview
    assert '"force": true' in preview


def test_mcp_registry_and_validation() -> None:
    registry = ToolRegistry()
    tool_schema = MCPToolSchema.create(
        server_name="db",
        tool_name="query",
        description="Query db",
        parameters={"type": "object", "properties": {"sql": {"type": "string"}}},
        is_read_only=True,
    )
    registry.register_mcp_tools((tool_schema,))

    assert "mcp.db.query" in registry.tool_names

    # 正常参数校验
    args = registry.validate("mcp.db.query", '{"sql": "SELECT 1"}')
    assert isinstance(args, MCPToolArgs)
    assert args.arguments == {"sql": "SELECT 1"}

    # 空参数默认转为空字典
    empty_args = registry.validate("mcp.db.query", "")
    assert isinstance(empty_args, MCPToolArgs)
    assert empty_args.arguments == {}

    # 未注册的 MCP 工具
    unregistered = registry.validate("mcp.db.drop_all", "{}")
    assert isinstance(unregistered, ValidationFailure)
    assert unregistered.code == "unknown_tool"

    # 非法 JSON 参数
    invalid_json = registry.validate("mcp.db.query", "{invalid")
    assert isinstance(invalid_json, ValidationFailure)
    assert invalid_json.code == "invalid_arguments"

    # 非字典 JSON 载荷
    invalid_type = registry.validate("mcp.db.query", "[1, 2, 3]")
    assert isinstance(invalid_type, ValidationFailure)
    assert invalid_type.code == "invalid_arguments"


@pytest.mark.asyncio
async def test_mcp_tool_pipeline_integration(tmp_path: Path) -> None:
    from haven.adapters.memory_session import MemorySessionStore
    from haven.adapters.process_executor import ProcessExecutor
    from haven.adapters.workspace_fs import FsWorkspace
    from haven.application.emitter import EventEmitter
    from haven.application.state import RunContext
    from haven.application.tool_pipeline import ToolPipeline
    from haven.contracts.model import ToolCallProposal
    from haven.domain.budget import Budget
    from haven.domain.enums import ApprovalDecision, RunStatus, ToolErrorCode, ToolStatus
    from haven.domain.ids import RunId, ToolCallId

    class SimpleResponder:
        def __init__(self, decision: ApprovalDecision) -> None:
            self.decision = decision

        async def respond(self, request: object) -> ApprovalDecision:
            return self.decision

    # 1. 启动 Mock MCP Client Manager
    config = MCPServerConfig(
        name="test_srv",
        command=sys.executable,
        args=("-c", MOCK_MCP_SERVER_SCRIPT),
        allowed_tools=("lookup", "execute_mutation"),
        read_only_tools=("lookup",),
    )
    mcp_manager = MCPClientManager((config,))
    tools = await mcp_manager.initialize()

    registry = ToolRegistry()
    registry.register_mcp_tools(tools)

    workspace = FsWorkspace(tmp_path)
    store = MemorySessionStore()
    emitter = EventEmitter(store, [])
    executor = ProcessExecutor()

    ctx = RunContext(
        run_id=RunId("run-mcp-test"),
        goal="test mcp gateway",
        mode=PermissionMode.INTERACTIVE,
        budget=Budget(),
    )
    ctx.move_to(RunStatus.RUNNING_MODEL)

    try:
        # A. 测试只读工具自动放行执行
        pipeline = ToolPipeline(
            workspace=workspace,
            executor=executor,
            store=store,
            emitter=emitter,
            approvals=SimpleResponder(
                ApprovalDecision.REJECTED
            ),  # 即使 responder 会拒绝，只读工具也不会触发审批
            registry=registry,
            recipes={},
            mode=PermissionMode.INTERACTIVE,
            scratch_dir=tmp_path / "scratch",
            mcp_manager=mcp_manager,
        )

        call_read = ToolCallProposal(
            call_id=ToolCallId("call-1"),
            tool_name="mcp.test_srv.lookup",
            arguments_json='{"query": "unit test"}',
        )
        ctx.move_to(RunStatus.VALIDATING_TOOL)
        res_read = await pipeline.execute(ctx, 1, call_read)
        assert res_read.result.status == ToolStatus.OK
        assert 'called lookup with {"query": "unit test"}' in str(res_read.result.payload)
        ctx.move_to(RunStatus.RUNNING_MODEL)

        # B. 测试写工具要求审批且审批通过
        pipeline_approve = ToolPipeline(
            workspace=workspace,
            executor=executor,
            store=store,
            emitter=emitter,
            approvals=SimpleResponder(ApprovalDecision.APPROVED),
            registry=registry,
            recipes={},
            mode=PermissionMode.INTERACTIVE,
            scratch_dir=tmp_path / "scratch",
            mcp_manager=mcp_manager,
        )
        call_write = ToolCallProposal(
            call_id=ToolCallId("call-2"),
            tool_name="mcp.test_srv.execute_mutation",
            arguments_json='{"action": "run_now"}',
        )
        ctx.move_to(RunStatus.VALIDATING_TOOL)
        res_write = await pipeline_approve.execute(ctx, 2, call_write)
        assert res_write.result.status == ToolStatus.OK
        assert 'called execute_mutation with {"action": "run_now"}' in str(res_write.result.payload)
        ctx.move_to(RunStatus.RUNNING_MODEL)

        # C. 测试写工具要求审批但审批被用户拒绝
        pipeline_reject = ToolPipeline(
            workspace=workspace,
            executor=executor,
            store=store,
            emitter=emitter,
            approvals=SimpleResponder(ApprovalDecision.REJECTED),
            registry=registry,
            recipes={},
            mode=PermissionMode.INTERACTIVE,
            scratch_dir=tmp_path / "scratch",
            mcp_manager=mcp_manager,
        )
        ctx.move_to(RunStatus.VALIDATING_TOOL)
        res_rejected = await pipeline_reject.execute(ctx, 3, call_write)
        assert res_rejected.result.status == ToolStatus.ERROR
        assert res_rejected.result.error_code == ToolErrorCode.APPROVAL_REJECTED

    finally:
        await mcp_manager.close_all()


@pytest.mark.asyncio
async def test_run_service_mcp_wiring(tmp_path: Path) -> None:
    from haven.adapters.memory_session import MemorySessionStore
    from haven.adapters.process_executor import ProcessExecutor
    from haven.adapters.workspace_fs import FsWorkspace
    from haven.application.emitter import EventEmitter
    from haven.application.run_service import RunService
    from haven.domain.budget import Budget
    from haven.domain.enums import ApprovalDecision

    config = MCPServerConfig(
        name="test_srv",
        command=sys.executable,
        args=("-c", MOCK_MCP_SERVER_SCRIPT),
        allowed_tools=("lookup",),
        read_only_tools=("lookup",),
    )
    store = MemorySessionStore()
    emitter = EventEmitter(store, [])
    workspace = FsWorkspace(tmp_path)

    class DummyModel:
        model_name = "test-model"

    class SimpleResponder:
        async def respond(self, request: object) -> ApprovalDecision:
            return ApprovalDecision.APPROVED

    run_service = RunService(
        model=DummyModel(),  # type: ignore[arg-type]
        workspace=workspace,
        executor=ProcessExecutor(),
        store=store,
        emitter=emitter,
        approvals=SimpleResponder(),
        recipes={},
        mode=PermissionMode.INTERACTIVE,
        budget=Budget(),
        mcp_servers=(config,),
    )
    try:
        assert len(run_service._mcp_manager.tools) == 0
        discovered = await run_service._mcp_manager.initialize()
        assert len(discovered) == 1
        assert discovered[0].namespaced_name == "mcp.test_srv.lookup"
    finally:
        await run_service.close()
        assert len(run_service._mcp_manager.tools) == 0


@pytest.mark.asyncio
async def test_mcp_client_drains_stderr_properly(tmp_path: Path) -> None:
    import asyncio

    script = (
        "import sys, json\n"
        "sys.stderr.write('debug warning log line 1\\n')\n"
        "sys.stderr.flush()\n"
        "for line in sys.stdin:\n"
        "    req = json.loads(line.strip())\n"
        "    if req.get('method') == 'initialize':\n"
        "        payload = {\n"
        "            'jsonrpc': '2.0',\n"
        "            'id': req.get('id'),\n"
        "            'result': {\n"
        "                'protocolVersion': '2024-11-05',\n"
        "                'capabilities': {},\n"
        "                'serverInfo': {'name': 'test', 'version': '1'}\n"
        "            }\n"
        "        }\n"
        "        sys.stdout.write(json.dumps(payload) + '\\n')\n"
        "        sys.stdout.flush()\n"
    )
    srv_file = tmp_path / "stderr_srv.py"
    srv_file.write_text(script)
    cfg = MCPServerConfig(
        name="stderr_srv",
        command=sys.executable,
        args=(str(srv_file),),
        allowed_tools=(),
    )
    client = MCPClient(cfg)
    try:
        await client.start()
        # 等待后台任务排空 stderr
        await asyncio.sleep(0.1)
        assert "debug warning log line 1" in client.last_stderr
    finally:
        await client.close()
