"""Default-tier Moonshine availability probe.

The default STT tier runs Moonshine in the **standalone shim subprocess**
(``stt_server.py``, tmux ``agentwire-stt``, ``:8101``), which the portal
auto-manages via ``ensure_managed_stt``. Process isolation keeps the ~19s
ONNX warm-up off the portal's event loop. This module no longer loads any
model in-process — it only exposes the spawn gate (``moonshine_importable``)
and the default model name shared with the shim.
"""

import importlib.util
import os

# Default Moonshine variant. moonshine/base balances speed and accuracy on
# CPU; moonshine/tiny is faster/lighter. Operator override via MOONSHINE_MODEL
# mirrors the standalone shim (stt_server.py).
DEFAULT_MOONSHINE_MODEL = os.environ.get("MOONSHINE_MODEL", "moonshine/base")


def moonshine_importable() -> bool:
    """True if useful-moonshine-onnx is installed (base install, py<3.14).

    Gates whether the portal bothers spawning the host STT shim for the
    default tier — on py3.14+ (no package) it stays the browser path.
    """
    return importlib.util.find_spec("moonshine_onnx") is not None
