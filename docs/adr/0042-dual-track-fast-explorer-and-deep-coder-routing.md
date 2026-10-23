# ADR 0042: Dual-Track Fast-Explorer & Deep-Coder Hybrid Model Routing

Date: 2026-10-23
Status: Accepted

## Context

Modern agentic software engineering tasks consist of two fundamentally different phases:
1. **Exploration & Navigation**: Searching repositories (`repo.search`), reading directory structures (`repo.list`), scanning file signatures (`repo.outline`, `repo.read`), and searching documentation (`web.search`). These steps are latency-sensitive, token-heavy, and do not require heavy reasoning. Running frontier reasoning models (e.g., DeepSeek-R1, Claude 3.7 Sonnet Thinking, o1/o3) for read-only exploration incurs unnecessary latency (often 10–30s per turn) and consumes significant reasoning token budgets.
2. **Implementation, Patching & Verification**: Generating precise syntactic edits (`repo.edit`, `repo.create`, `repo.apply_patch`), diagnosing complex compiler/test failures (`repo.check`), and satisfying the Evidence Gate. These steps demand maximum mathematical, logical, and code-reasoning capabilities. Using smaller models here often leads to hallucinations and syntax bugs.

Previously, Haven bound the entire session to a single configured provider model (`provider.model`), forcing a costly trade-off between speed and reasoning power.

## Decision

**Introduce a Dual-Track Model Router (`ModelRouter`) implementing the `ModelPort` protocol:**

1. **Deterministic Adaptive Routing (`haven.application.model_router`)**:
   - The application layer exposes `ModelRouter`, wrapping a fast exploration model (`fast_model`, e.g. `gpt-4o-mini`, `deepseek-chat`) and a deep reasoning model (`deep_model`, e.g. `deepseek-reasoner`, `claude-3-7-sonnet`).
   - `ModelRouter.select_model(ctx, step)` evaluates the turn state deterministically:
     - If `always_deep` is configured: routes 100% of turns to `deep_model`.
     - If `fast_turns` is configured (e.g. `fast_turns = 2`): routes turns `step <= fast_turns` to `fast_model`, then switches to `deep_model`.
     - If `ctx.ledger.edits` is non-empty (code mutations have commenced): immediately switches to `deep_model`.
     - Otherwise (read-only exploration and discovery): routes to `fast_model`.

2. **Single Pipeline Invariant**:
   - `ModelRouter` implements `ModelPort` directly.
   - The core runtime (`RunService`), context builder (`ContextBuilder`), tool execution pipeline (`ToolPipeline`), and approval policy remain single-channel and unaware of provider switches.
   - The state machine transitions and audit events remain strictly deterministic.

3. **Accurate Per-Model Telemetry & Token Accounting**:
   - `RunTelemetry.charge_usage` is updated to support dynamic per-model pricing overrides.
   - When a turn completes, usage tokens are calculated against the specific active model's pricing profile rather than a static global config, ensuring exact cost accounting across mixed-model sessions.

4. **Multi-Source Configuration & CLI Support**:
   - Configurable via TOML:
     ```toml
     [router]
     enabled = true
     fast_model = "gpt-4o-mini"
     deep_model = "deepseek-reasoner"
     fast_turns = 2
     ```
   - Configurable via environment variables (`HAVEN_FAST_MODEL`, `HAVEN_DEEP_MODEL`, `HAVEN_FAST_TURNS`).
   - Configurable via CLI flags: `--fast-model` and `--deep-model` in `haven run`.

## Consequences

- Read-only exploration and file browsing achieve sub-second TTFT and minimal cost overhead.
- Deep reasoning models are deployed when reasoning counts: generating code diffs, analyzing compiler diagnostics, and fulfilling the Evidence Gate.
- Token budgets stretch further without sacrificing code quality or test pass rates.
- No changes to existing adapters or external interfaces; legacy configurations continue to operate seamlessly with a single model.
