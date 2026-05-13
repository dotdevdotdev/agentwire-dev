"""Agentwire REPL — interactive harness built on claude-agent-sdk.

This package is the implementation of the `sdk-bypass`, `sdk-prompted`, and
`sdk-restricted` session types. It is invoked by build_agent_command when a
session is spawned via `agentwire new --type sdk-*`; users do not call it
directly.
"""
