"""TTS Engine Implementations.

Engines resolve lazily (PEP 562) so importing one engine never pulls in the
others' dependencies — the torch-free kokoro path must stay importable in the
base install, where torch (chatterbox/zonos) is absent.
"""

_ENGINES = {
    "ChatterboxEngine": ".chatterbox",
    "KokoroEngine": ".kokoro",
    "ZonosHybridEngine": ".zonos",
    "ZonosTransformerEngine": ".zonos",
}

__all__ = list(_ENGINES)


def __getattr__(name: str):
    if name in _ENGINES:
        import importlib

        module = importlib.import_module(_ENGINES[name], __name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
