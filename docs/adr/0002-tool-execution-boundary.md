# ADR 0002: Tool Execution Boundary

## Status

Accepted

## Context

Coding agents combine non-deterministic model output with deterministic, security-sensitive filesystem and process side effects. Letting the model execute actions directly is unsafe and hard to audit.

## Decision

All model-proposed file and process actions flow through one pipeline:

1. Tool registry lookup (name + version)
2. Strict schema validation
3. Workspace fact collection (canonical paths, preimage, risk)
4. Deterministic policy (`allow` / `ask` / `deny`)
5. Exact approval when policy returns `ask`
6. Internal `ExecutionTicket` issuance
7. Executor runs only from `ExecutionTicket`
8. Structured `ToolResult`, evidence, and trace events

Invariants:

- Executor never accepts raw model JSON
- Approval is bound to tool, args, preimage, and preview digest; stale approvals are rejected
- Policy cannot be widened by user natural language or repo-local prompt injection
- Success requires programmatic evidence (diff, postimage, checks), not model self-reporting

## Consequences

- More implementation work than a thin tool-calling wrapper
- Clear audit trail and replay surface
- Safer default for interactive editing with human approval

## Alternatives considered

- **Direct tool execution after JSON parse**: rejected — no stable policy or approval binding
- **Persistent broad grants**: rejected for MVP — too easy to over-approve
- **Arbitrary shell**: rejected — unbounded risk and poor eval reproducibility
