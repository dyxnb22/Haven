# ADR 0039: Hardened MCP Gateway with Fail-Closed Schema Pinning

Date: 2026-10-23
Status: Accepted

## Context

By 2026, the Model Context Protocol (MCP) emerged as the industry standard for extending agent capabilities with external data sources and specialized developer services.
However, naive integration of MCP into an autonomous coding agent introduces grave security vulnerabilities:
1. **Dynamic Schema Drift & Privilege Escalation**: External MCP servers can dynamically mutate their parameter schemas between turns, injecting malicious default arguments or redefining parameters to trick human approvers.
2. **Namespace Collision & Tool Shadowing**: External tools could attempt to override built-in core primitives (`repo.read`, `repo.edit`, `repo.exec`), hijacking the control flow.
3. **Ambiguous Side Effects**: External tools lack standardized read/write classification, risking unintended mutations (e.g. dropping a database table) without human approval.
4. **Evidence Gate Tampering**: If external tools could simulate verification evidence, prompt-injected models could bypass Haven's strict verification guarantee.

## Decision

**Implement a Hardened MCP Gateway conforming to Haven's core safety invariants:**

1. **Static Configuration Declaration**:
   - MCP servers must be declared statically in user (`~/.haven/config.toml`) or project (`.haven.toml`) configuration under `[mcp.servers.<name>]`. Dynamic, in-session mounting by the model is strictly forbidden.

2. **Namespace Isolation**:
   - All external tools are mapped into a dedicated namespace: `mcp.<server_name>.<tool_name>`.
   - Core built-in tools (`KNOWN_TOOLS`) remain compiled-in and immutable; collisions are impossible.

3. **Strict Allowlist Filtering (`allowed_tools`)**:
   - An MCP server may report many tools via `tools/list`, but Haven exposes ONLY the tools explicitly declared in `allowed_tools`. Unlisted tools are dropped immediately.

4. **Fail-Closed Schema Pinning (`pinned_schemas`)**:
   - Configurations may specify the expected SHA-256 digest of canonical tool parameter schemas.
   - If an external server returns an unexpected schema, the gateway flags it as drifted (`mcp_schema_drift`). Policy evaluation deterministically issues a `DENY` (`mcp_schema_drift`), preventing any execution or approval bypass.

5. **Strict Deterministic Policy & Approval**:
   - Tools listed in `read_only_tools` are evaluated as `PolicyDecision.ALLOW` (`RiskLevel.LOW`).
   - All other MCP tools default to high-risk mutating tools and require explicit approval: `PolicyDecision.ASK` (`RiskLevel.HIGH`).
   - In `PermissionMode.READ_ONLY`, mutating MCP tools are hard-denied (`DENY`).
   - `ApprovalCardRenderer` formats dedicated preview cards displaying the target server, tool name, and formatted payload parameters.

6. **Single Execution Channel & Evidence Gate Invariant**:
   - All MCP tool calls pass through the single `ToolPipeline`: schema validation, facts collection, deterministic policy, approval, minted ticket, and process execution.
   - MCP tool outputs are purely observations (`<tool_output>`) for the model. They NEVER constitute verification evidence for the Evidence Gate (only `repo.diff` and passing `repo.check` can satisfy completion).

7. **Process Lifecycle & Orphan Cleanup**:
   - Stdio JSON-RPC 2.0 subprocesses are managed by `MCPClientManager` and cleanly terminated upon session close, abort, or exception.

## Consequences

- Haven can securely interoperate with external MCP ecosystems (e.g., GitHub, issue trackers, database inspectors, specialized linters).
- Haven's single execution channel, deterministic policy, and Evidence Gate invariants remain 100% intact.
