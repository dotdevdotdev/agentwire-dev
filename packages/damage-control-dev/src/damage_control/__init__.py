"""Damage control for AI coding agents.

This module stays import-free on purpose: ``python -I -m damage_control.hook`` imports
the package before it runs a line of ``hook.py``, and an exception raised here would
escape the hook's fail-closed wrapper (a Python crash exits 1, which Claude Code treats
as non-blocking).
"""

__version__ = "0.1.0"
