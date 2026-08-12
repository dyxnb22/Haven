"""Interfaces layer: the CLI (Typer) and the TUI (Textual).

Everything here is presentation. An interface turns user intent into
application-service calls and renders the event stream; it never executes
tools, never evaluates policy, and never talks to a provider directly.
Import-linter enforces that this package reaches adapters only through
`haven.bootstrap` (the composition root), so swapping a UI cannot change
any security-relevant behavior.
"""
