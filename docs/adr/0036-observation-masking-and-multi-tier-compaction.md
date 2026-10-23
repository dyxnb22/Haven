# ADR 0036: observation masking and multi-tier deterministic compaction

Date: 2026-10-22
Status: Accepted

## Context

Haven's context management was built around two decisions:
1. **Prompt-cache prefix stability (ADR 0008)**: The fixed head (system rules, AGENTS.md guidance, user goal) and the append-only transcript form a byte-stable prefix across turns so provider prompt caching can hit at 85–95%.
2. **Deterministic compaction (ADR 0010, ADR 0024)**: When the transcript exceeds the character budget (`MAX_CONTEXT_CHARS`), the oldest tool-call units are dropped and replaced by a program-assembled digest (`run_digest`) of recorded facts (paths, SHA digests, exit codes). The model is never asked to summarize, preventing hallucinations from establishing false permissions.

However, ADR 0010 had an all-or-nothing drawback:
When an older tool output (such as reading a 1,000-line file with `repo.read` or viewing build stderr from `repo.exec`) pushed the transcript over the limit, the *entire* tool turn was removed from the message array.
This destroyed causal context: the model lost the class definitions, error stack traces, and verification details it had observed, leading to loops where the model re-read files it had previously read.

Mature peers like Codex CLI, OpenCode, and Claude Code employ multi-tier compaction where bulky observations are masked or folded first before conversation history is dropped.

## Decision

**Implement Observation Masking as Tier 1 in a Multi-Tier Deterministic Compaction Pipeline:**

1. **`fold_text(text, head_lines=4, tail_lines=4, threshold_chars=400)`**:
   - Compresses oversized string payloads (`content`, `stdout`, `output`, `diff`) into head lines + omission markers (`[... N lines folded (... chars); re-read on demand ...]`) + tail lines.
   - Preserves start-of-file declarations (imports, class/function definitions) and end-of-file traces (error causes, return signatures).

2. **`mask_bulky_observations` & Tier 1 Masking in `summarize_dropped`**:
   - Walks candidate droppable units from oldest to newest;
   - Folds oversized observations in older units while keeping recent units (last 2 tool outputs) intact;
   - Halts folding as soon as `total <= limit`. This guarantees that subsequent turns within budget do not retroactively fold older turns, preserving ADR 0008 prompt-cache prefix stability;
   - Keeps the assistant tool call and tool result pair intact in the message list, maintaining the causal chain.

3. **Multi-Tier Compaction Hierarchy**:
   - **Tier 1 (Observation Masking)**: Fold bulky observations in candidate units. If context fits, retain all turns without generating a digest.
   - **Tier 2 (Structured Run Digest, ADR 0010)**: If transcript still exceeds budget after folding, drop oldest units into a program-generated `run_digest`.
   - **Tier 3 (Hard Clamp, ADR 0024)**: `enforce_hard_limit` ensures total context never exceeds the budget, prioritizing eviction of non-user units (narrative assistant turns, excess tool stubs) before touching earlier user/gate instructions.

## Consequences

- Prevents context truncation loops caused by premature dropping of large observations.
- Preserves prefix cache hit rates by strictly enforcing monotonic, demand-driven folding from oldest to newest.
- Backward compatibility: when observation masking is not triggered or for non-bulky content, Tier 2 and Tier 3 behave byte-for-byte identically to existing baselines.
