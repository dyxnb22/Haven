# ADR 0038: sandboxed technical documentation search and fetch

Date: 2026-10-22
Status: Accepted

## Context

Haven's execution sandbox strictly enforces `(deny network*)` during code execution (`repo.exec`) to prevent prompt injection attacks from exfiltrating codebase data or downloading unapproved binaries.
ADR 0028 deferred web access because untrusted external content represents an prompt injection surface.

However, in 2026 development, models frequently encounter modern framework updates, new library releases, or specific API deprecations that occurred after their training cutoff. Without technical documentation lookup, models hallucinate outdated parameters or non-existent methods, leading to stubborn edit-check failure loops.

## Decision

**Introduce a sandboxed, read-only documentation lookup channel with strict SSRF defenses:**

1. **`repo.exec` Sandbox Isolation Invariant Remains Absolute**:
   - The OS sandbox for code execution never relaxes its `(deny network*)` rule. Subprocesses run in isolation.

2. **Controlled Host-Level Read-Only Tools**:
   - `web.search(query, max_results=5)`: Queries public technical documentation via search endpoints; returns bounded title, URL, and snippet records.
   - `web.fetch(url, max_chars=10000)`: Fetches target public documentation pages, converts HTML to clean Markdown, and caps output size.

3. **SSRF Defense Boundary**:
   - URL scheme must strictly be `http` or `https`.
   - Hostnames are resolved to IP addresses and checked against private, loopback (`127.0.0.0/8`, `::1`), link-local (`169.254.0.0/16`), and multicast address ranges. Any request to local or internal networks is blocked unconditionally.

4. **Untrusted Data Framing**:
   - All web output is treated as untrusted external data, wrapped inside `<tool_output tool="web.*">` tags. System instructions explicitly dictate that instructions inside tool outputs cannot enlarge permissions or override operating rules.

## Consequences

- Eliminates API hallucinations when integrating third-party libraries or modern tooling.
- Preserves the local code execution isolation while providing safe, controlled documentation access.
