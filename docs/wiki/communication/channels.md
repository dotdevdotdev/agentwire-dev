> Living document. Update this, don't create new versions.

# Channels — Developer Guide

AgentWire channels are **outbound-only notification integrations**. They let a session push a notification out (email, SMS) without exposing any inbound surface. Two channels ship: **email** (Resend) and **quo** (OpenPhone SMS).

> Inbound bridges (Telegram, Discord, Slack) were removed to keep the wire surface outbound-only. The portal remains the primary push-to-talk surface for inbound user input.

## How a channel works

A channel is a `SendOnlyChannel` subclass registered with `ChannelRegistry`. It owns its config dataclass, exposes a `send()` coroutine, and a thin CLI handler in `agentwire/__main__.py`.

```python
@ChannelRegistry.register("my_channel")
class MyChannel(SendOnlyChannel):
    name = "my_channel"
    config_class = MyConfig
    config_key = "my_channel"

    async def send(self, text, **kwargs) -> ChannelResult:
        # Send the message; return ChannelResult(success, message_id, error)
        ...
```

## Config

Each channel defines its own dataclass and reads from `channels.{config_key}:` in `~/.agentwire/config.yaml`. **Secrets are env-only** — the key comes from an env var (one line in `~/.agentwire/.env`, see [Secrets & API keys](../security/secrets.md)), never from config.yaml. Declare it as a non-init field so yaml can't supply it:

```python
@dataclass
class MyConfig:
    default_to: str = ""
    api_key: str = field(init=False, default="")

    def __post_init__(self):
        self.api_key = os.environ.get("MY_API_KEY", "")
```

```yaml
# ~/.agentwire/config.yaml
channels:
  my_channel:
    default_to: "user@example.com"
```

```bash
# ~/.agentwire/.env
MY_API_KEY=your-key
```

The channel registry resolves config from YAML automatically — no `config.py` changes needed. Unknown yaml fields (e.g. a stale `api_key:`) are ignored with a warning rather than crashing config load.

## CLI integration

```python
# in agentwire/__main__.py
from agentwire.channels.my_channel import cmd_my_channel
my_parser = subparsers.add_parser("my_channel", help="...")
my_parser.add_argument("--body", "-b", type=str, help="Message body")
my_parser.add_argument("--to", type=str, help="Recipient (overrides default_to)")
my_parser.add_argument("-q", "--quiet", action="store_true")
my_parser.set_defaults(func=cmd_my_channel)
```

`cmd_my_channel` lives in the channel module, reads `args.body` (or stdin), calls `send_my_thing(...)`, and prints/JSONs the result.

## MCP tools

```python
@mcp.tool()
def my_channel_send(text: str, to: str | None = None) -> str:
    data = run_agentwire_cmd(["my_channel", "--body", text])
    if data.get("success"):
        return "Sent."
    return f"Error: {data.get('error')}"
```

## Built-in channels

| Channel | Library | Config key | Purpose |
|---------|---------|------------|---------|
| Email | resend | `email` | Branded HTML notifications via Resend |
| Quo | stdlib | `quo` | SMS via OpenPhone API |

## Testing checklist

- [ ] `agentwire channels list` shows your channel
- [ ] Config loads correctly from YAML
- [ ] Key is read from its env var (`~/.agentwire/.env`)
- [ ] `send()` returns success with valid config
- [ ] `send()` returns a clear error with missing/invalid config
- [ ] CLI command works (`agentwire my_channel --body "test"`)
- [ ] MCP tool works (if added)

## Optional dependencies

Channels with external deps use try/except so the import doesn't blow up when the dep isn't installed:

```python
try:
    import resend
except ImportError:
    resend = None
```

`send()` returns a clear error if the dep is missing; `channels list` still shows the channel.

## Security

- API keys live in `~/.agentwire/.env` only ([Secrets & API keys](../security/secrets.md)) — `RESEND_API_KEY` for email, `QUO_API_KEY` for quo. Config never holds a key, so the portal's config editor never round-trips one.
- Each channel only reads from its own `channels.{config_key}:` slot — no cross-channel config peeking.
- Outbound-only means there's no public webhook endpoint on the portal for a channel to attack. The portal's `/ws/{session}` is the only WS surface; channels never expose HTTP.
