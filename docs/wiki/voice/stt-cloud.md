# Cloud STT (`stt.backend: cloud`)

> Living document. Update this, don't create new versions.

The cloud tier serves the "my server can't run local STT well / phone has no
browser STT" case (#280) with **zero extra daemons**: the browser uploads
audio to the portal's `/transcribe` (same as the custom tier), the portal
decodes it to 16 kHz mono WAV in-process via PyAV, then POSTs it directly to
a hosted transcription API. No shim process, no new ports.

## Configuration

```yaml
# ~/.agentwire/config.yaml
stt:
  backend: cloud
  cloud:
    base_url: "https://api.openai.com/v1"   # any OpenAI-compatible endpoint
    model: "gpt-4o-mini-transcribe"
    api_key_env: "OPENAI_API_KEY"           # NAME of the env var holding the key
    language: ""                            # optional ISO-639-1 hint
  timeout: 30
```

Every `cloud.*` field is optional — the defaults above are what you get with
just `backend: cloud`. The portal reads the key from the env var named by
`api_key_env` at startup and **refuses to start** if it's missing (fail fast
beats silent dead mics). `agentwire doctor` checks the same thing.

**Where to put the key:** `~/.agentwire/.env` — agentwire loads it on every
startup (`load_dotenv` in `__main__.py`), so a line like
`OPENAI_API_KEY=sk-...` is all it takes. It's also covered by the
damage-control hooks (zero-access for agents). A shell-profile `export`
works too, but the `.env` file is the blessed spot.

## It's generic: any OpenAI-compatible provider

The protocol — multipart `file` + `model` to `{base_url}/audio/transcriptions`
with a Bearer key, JSON `{"text": ...}` back — is the de-facto industry
standard, so `base_url` + `api_key_env` point it anywhere:

| Provider | `base_url` | `model` (example) | `api_key_env` |
|---|---|---|---|
| OpenAI | `https://api.openai.com/v1` (default) | `gpt-4o-mini-transcribe` (~$0.003/min), `whisper-1` | `OPENAI_API_KEY` |
| Groq | `https://api.groq.com/openai/v1` | `whisper-large-v3-turbo` | `GROQ_API_KEY` |
| Mistral | `https://api.mistral.ai/v1` | `voxtral-mini-latest` | `MISTRAL_API_KEY` |
| Self-hosted OpenAI-compatible server (speaches, LocalAI, whisper.cpp server) | wherever it listens | per server | whatever you set |

Providers with their own protocol (Deepgram, AssemblyAI, ...) don't fit this
tier — wrap them in a [custom shim](shim-contract.md) instead. That's the
three-tier story: `default` = zero setup in the browser, `cloud` = any
OpenAI-compatible API, `custom` = literally anything behind your own shim.

## Security posture

- The key lives only in the **portal process environment**; config holds the
  env var's *name*, never the key.
- It's sent only in the server-side `Authorization` header of the provider
  request — never to the browser, never echoed by any portal endpoint.
- No runtime backend-switching API: changing tiers/providers is config +
  portal restart, same as everything else.
- Trade-off vs. local backends: your audio is uploaded to the provider.

## Source

- Backend: `agentwire/stt/cloud.py` (`CloudSTTBackend`)
- Tier selection: `agentwire/stt/__init__.py::get_stt_backend`
- Config: `agentwire/config.py::STTConfig`
- Tests: `tests/unit/test_stt_cloud.py`
