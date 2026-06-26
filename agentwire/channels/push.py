"""Web Push channel — send-only via VAPID + the browser Push API (#483).

This is the native, carrier-free, zero-config notification surface for the
portal: a push reaches a backgrounded/locked phone through the OS push service
(APNs/FCM) without keeping a tab awake, as long as the device has installed the
PWA (add-to-home-screen) and granted notification permission.

Two halves live elsewhere:

* The **client** (service worker + page) registers a subscription and POSTs it to
  ``/api/push/subscribe``; it is persisted by :mod:`agentwire.push_store`.
* This module is the **sender**: it signs each push with the owner's VAPID
  private key and fans out to every stored subscription, pruning the ones the
  push service reports as gone (404/410).

Activation is gated on config + keys:

* ``channels.push.enabled: true`` in ``~/.agentwire/config.yaml`` (default False).
* ``VAPID_PRIVATE_KEY`` / ``VAPID_PUBLIC_KEY`` in ``~/.agentwire/.env`` (env-only,
  like every other secret — never config.yaml). Generate a pair with
  ``agentwire push keygen``.
* ``pywebpush`` installed (ships in the base install). The import is still
  guarded so the portal degrades to "push disabled" rather than crashing if it
  is somehow absent.

With ``enabled`` False, or keys missing, :func:`send_web_push` is a no-op that
reports why — the portal keeps working, it just doesn't push.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Optional

try:
    from pywebpush import WebPushException, webpush
except ImportError:  # pragma: no cover - optional dependency
    webpush = None
    WebPushException = Exception

from .. import push_store
from .base import (
    ChannelRegistry,
    ChannelResult,
    SendOnlyChannel,
)


@dataclass
class PushConfig:
    """Web Push configuration.

    The VAPID keypair is env-only (``VAPID_PRIVATE_KEY`` / ``VAPID_PUBLIC_KEY``
    in ``~/.agentwire/.env``); only the on/off switch and the contact subject
    live in config.yaml.
    """

    enabled: bool = False
    # mailto: or https: identifying the push sender to the push service.
    vapid_subject: str = "mailto:owner@agentwire.dev"
    vapid_private_key: str = field(init=False, default="")
    vapid_public_key: str = field(init=False, default="")

    def __post_init__(self):
        self.vapid_private_key = os.environ.get("VAPID_PRIVATE_KEY", "")
        self.vapid_public_key = os.environ.get("VAPID_PUBLIC_KEY", "")


@dataclass
class PushSendResult:
    """Outcome of a fan-out push send."""

    success: bool
    sent: int = 0
    pruned: int = 0
    error: Optional[str] = None


def _get_push_config() -> PushConfig:
    from agentwire.config import get_config

    config = get_config()
    cfg = config.channels.get("push")
    return cfg if cfg else PushConfig()


def push_ready() -> tuple[bool, str]:
    """Whether push can actually send right now, plus a human reason if not."""
    if webpush is None:
        return False, "pywebpush not installed (pip install pywebpush)"
    cfg = _get_push_config()
    if not cfg.enabled:
        return False, "channels.push.enabled is false"
    if not cfg.vapid_private_key or not cfg.vapid_public_key:
        return False, "VAPID keys missing from ~/.agentwire/.env (run: agentwire push keygen)"
    return True, "ready"


def send_web_push(
    title: str,
    body: str = "",
    url: str = "/",
    tag: Optional[str] = None,
) -> PushSendResult:
    """Fan a push out to every stored subscription.

    No-op (success=False with a reason) when push is disabled or unconfigured —
    callers fire-and-forget, so this never raises for the common "not set up"
    case. Subscriptions the push service reports as gone (404/410) are pruned.
    """
    ready, reason = push_ready()
    if not ready:
        return PushSendResult(success=False, error=reason)

    cfg = _get_push_config()
    subs = push_store.load()
    if not subs:
        return PushSendResult(success=True, sent=0, error="no subscriptions")

    payload = json.dumps(
        {"title": title, "body": body, "url": url, "tag": tag or "agentwire"}
    )
    vapid_claims = {"sub": cfg.vapid_subject}

    sent = 0
    dead: list[str] = []
    last_error: Optional[str] = None
    for sub in subs:
        try:
            webpush(
                subscription_info=sub.to_webpush(),
                data=payload,
                vapid_private_key=cfg.vapid_private_key,
                vapid_claims=dict(vapid_claims),
                timeout=10,
            )
            sent += 1
            push_store.touch(sub.endpoint)
        except WebPushException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (404, 410):
                # Subscription expired / unsubscribed — garbage-collect it.
                dead.append(sub.endpoint)
            else:
                last_error = str(e)
        except Exception as e:  # pragma: no cover - defensive
            last_error = str(e)

    pruned = push_store.prune(dead)
    return PushSendResult(
        success=sent > 0 or not subs,
        sent=sent,
        pruned=pruned,
        error=last_error,
    )


def generate_vapid_keys() -> tuple[str, str]:
    """Generate a fresh (private, public) VAPID keypair, base64url-encoded.

    The private key is the application-server-key seed py_vapid persists; the
    public key is what the browser needs as ``applicationServerKey``. Both are
    raw base64url strings suitable for ``~/.agentwire/.env``.
    """
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())

    # Private: raw 32-byte scalar, base64url (the form py_vapid/pywebpush accept).
    private_value = private_key.private_numbers().private_value
    priv_bytes = private_value.to_bytes(32, "big")
    priv_b64 = base64.urlsafe_b64encode(priv_bytes).decode("utf-8").rstrip("=")

    # Public: uncompressed point (0x04 || X || Y), base64url — applicationServerKey.
    pub_bytes = private_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    pub_b64 = base64.urlsafe_b64encode(pub_bytes).decode("utf-8").rstrip("=")

    return priv_b64, pub_b64


def cmd_push(args) -> int:
    """CLI handler for the ``push`` command (keygen / status)."""
    sub = getattr(args, "push_cmd", None)

    if sub == "keygen":
        try:
            priv, pub = generate_vapid_keys()
        except Exception as e:
            print(f"Error generating keys: {e}", file=sys.stderr)
            return 1
        print("# Add these two lines to ~/.agentwire/.env (chmod 600):")
        print(f"VAPID_PRIVATE_KEY={priv}")
        print(f"VAPID_PUBLIC_KEY={pub}")
        print()
        print("# Then enable push in ~/.agentwire/config.yaml:")
        print("#   channels:")
        print("#     push:")
        print("#       enabled: true")
        print("#       vapid_subject: mailto:you@example.com")
        return 0

    # default: status
    ready, reason = push_ready()
    cfg = _get_push_config()
    subs = push_store.load()
    print(f"Web Push: {'READY' if ready else 'NOT READY'} ({reason})")
    print(f"  enabled:         {cfg.enabled}")
    print(f"  vapid keys set:  {bool(cfg.vapid_private_key and cfg.vapid_public_key)}")
    print(f"  subscriptions:   {len(subs)}")
    return 0


@ChannelRegistry.register("push")
class PushChannel(SendOnlyChannel):
    """Web Push send-only channel (VAPID)."""

    name = "push"
    config_class = PushConfig
    config_key = "push"

    async def send(self, text: str, **kwargs) -> ChannelResult:
        import asyncio

        result = await asyncio.to_thread(
            send_web_push,
            kwargs.get("title") or "AgentWire",
            text,
            kwargs.get("url", "/"),
            kwargs.get("tag"),
        )
        return ChannelResult(
            success=result.success,
            message_id=result.sent,
            error=result.error,
        )
