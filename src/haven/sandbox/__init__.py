"""Standalone helpers that re-exec a command under a kernel sandbox.

Nothing here may import the rest of Haven: these modules start inside a
scrubbed environment, moments before the target program replaces the process.
"""
