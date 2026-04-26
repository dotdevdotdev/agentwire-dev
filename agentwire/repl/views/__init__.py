"""Composite views for the Textual REPL.

Each view is a separate `textual.App` subclass that composes the SDK
primitives (`AgentwireSDKClient`, `StreamRenderState`, sinks) into a
specific UI shape. Today: fan-out N-column. Future: diff, multi-tool-pane,
conversation-tree.
"""
