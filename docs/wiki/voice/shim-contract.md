# Voice Shim Contract

> Living document. Update this, don't create new versions.

agentwire's voice backends are a tiered model: `default` (built into the
portal, zero setup), `custom` (bring your own model behind a small HTTP
shim), and — for STT — `cloud` (the portal POSTs audio straight to any
OpenAI-compatible transcription API; see
[stt-cloud.md](stt-cloud.md), no shim involved). This page is the contract
a custom shim implements. The test of this document: you should be able to
write a working shim from this page alone, without reading agentwire source.

## The tiers

| | `default` | `cloud` (STT only) | `custom` |
|---|---|---|---|
| STT | Chrome SpeechRecognition in the portal (Chrome is the blessed browser) + jargon-correction map | audio upload → portal → hosted transcription API (key from env, server-side only) | audio upload → your shim (`POST /transcribe`) |
| TTS | in-process Kokoro-82M (CPU, ~200MB auto-download on first portal start); browser `speechSynthesis` while the model warms up or if it can't load | — | text → your shim (`POST /tts`) → WAV broadcast |
| Setup | none | API key in the portal env | run your shim, point config at it |
| Quality | good neural voice (32 presets, 8 languages), ~90% semantic STT accuracy | provider-grade STT (e.g. `gpt-4o-mini-transcribe`, ~$0.003/min) | whatever your model can do — cloning, emotion control, GPU engines |

```yaml
# ~/.agentwire/config.yaml
tts:
  backend: custom
  url: http://localhost:8100
stt:
  backend: custom
  url: http://localhost:8101
```

## Design doctrine: envelope, not vocabulary

agentwire defines the **envelope**, never the model's vocabulary. The
mandatory surface is tiny — text in / audio out (TTS), audio in / text out
(STT). Everything model-specific rides in two opaque pass-through fields that
agentwire transports verbatim and never interprets:

- **`instructions`** (string) — free text, effectively a prompt riding along
  with the transaction: `"speak warmly, slightly amused"`. Set globally via
  `tts.instructions` / `stt.instructions` in config.
- **`options`** (object) — arbitrary JSON. Set via `tts.options` /
  `stt.options`. The bundled TTS shim, for example, reads
  `options.backend: kokoro` to pick its engine and `options.exaggeration`
  for chatterbox-style knobs.

Inline markup is the shim's business: if your model understands `[laughter]`
or `<emotion:happy>` tags embedded in `text`, consume them. If it doesn't,
**strip unknown markup rather than speaking it literally** — a capability-blind
caller must never produce audibly broken output. (agentwire's own `default`
tier strips standalone lowercase `[tag]` / `<tag:value>` tokens before OS or
browser synthesis.)

## TTS shim contract

### `POST /tts` (required)

Request (`application/json`):

```json
{
  "text": "Build finished. Two tests fixed.",
  "voice": "amy",
  "instructions": "speak warmly",
  "options": {"backend": "kokoro", "exaggeration": 0.5}
}
```

Only `text` is guaranteed present. `voice`, `instructions`, `options` are
optional — handle their absence.

Response: `200` with `Content-Type: audio/wav`, body = WAV bytes.
agentwire chunks long text into sentences client-side and calls `/tts` per
chunk, so latency per call matters more than long-text handling.

### `GET /health` (required)

`200` with any JSON body when ready to serve. agentwire probes this (1.5s
timeout) for availability reporting and fail-fast in the `say` tool.

### `GET /voices` (optional)

```json
{"voices": [{"name": "amy", "duration": 10.2}]}
```

Powers `agentwire voiceclone list` and voice pickers. Omit if your model has
no selectable voices.

### `GET /capabilities` (optional, recommended)

```json
{
  "tool_prompt": "This TTS supports inline [laugh], [sigh] tags; use sparingly.",
  "voices": ["amy", "default"],
  "emotion_control": true,
  "paralinguistic_tags": true,
  "languages": ["English"]
}
```

**`tool_prompt` is the most important field** — it closes the capability loop
at the producer. Discovery downstream is useless if the agent never learns
what to emit: at MCP-server start, agentwire appends `tool_prompt` verbatim to
the agent-facing `say` tool description, and at session creation it's appended
to the `voice` role prompt. You (the shim dev) write the prompt, the tooldef
teaches the agent, the agent emits your tags, the envelope passes them through
untouched, your shim renders them. agentwire stays a dumb pipe at every step.

**Caveat:** MCP tooldefs are read at MCP-server start (a separate process
launched by Claude Code). Swapping shims requires a session restart to
re-teach running agents.

### Optional extras the bundled shim implements

`POST /voices/{name}` (multipart WAV upload — voice cloning),
`DELETE /voices/{name}`, `GET /engines`, `POST /engines/{name}/load`
(hot-swap). None are part of the core contract.

## STT shim contract

### `POST /transcribe` (required)

Request: `multipart/form-data` with field `file` — WAV, 16 kHz mono PCM16
(agentwire decodes browser WebM/Opus and resamples before upload). When
configured, `instructions` and `options` (JSON-encoded string) arrive as
additional form fields — language hints, vocabulary biasing, whatever your
model takes. Ignore them if not.

Response:

```json
{"text": "refactor the auth module and run the tests"}
```

Extra keys (`language`, `duration`, …) are fine; agentwire reads `text`.

### `GET /health` (required)

`200` when ready. `{"status": "ok"}` by convention; agentwire treats any 200
as healthy.

### `GET /capabilities` (optional)

Same shape as TTS; `tool_prompt` is usually empty for STT.

## Reference shims (bundled)

Both bundled servers implement the full optional surface and are the worked
examples for this contract:

| Shim | Source | Run | Models |
|---|---|---|---|
| TTS | `agentwire/tts_server.py` | `agentwire tts start` (port 8100) | kokoro (CPU), chatterbox (GPU cloning), zonos (emotion via `instructions`) |
| STT | `agentwire/stt/stt_server.py` | `agentwire stt start` (port 8101) | moonshine ONNX (fast CPU), faster-whisper |

Smoke-test either with curl:

```bash
curl -s http://localhost:8100/health
curl -s http://localhost:8100/capabilities
curl -s -X POST http://localhost:8100/tts \
  -H 'Content-Type: application/json' \
  -d '{"text": "hello world"}' -o /tmp/out.wav && afplay /tmp/out.wav

curl -s http://localhost:8101/health
curl -s -X POST http://localhost:8101/transcribe -F file=@sample.wav
```

## A from-scratch shim in ~30 lines

Wrap any model or API. Example: a Deepgram STT shim.

```python
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["fastapi", "uvicorn", "httpx", "python-multipart"]
# ///
import os
import httpx
import uvicorn
from fastapi import FastAPI, File, UploadFile

app = FastAPI()
DEEPGRAM_KEY = os.environ["DEEPGRAM_API_KEY"]


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/transcribe")
async def transcribe(file: UploadFile = File(...)):
    audio = await file.read()
    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://api.deepgram.com/v1/listen?model=nova-3",
            headers={"Authorization": f"Token {DEEPGRAM_KEY}",
                     "Content-Type": "audio/wav"},
            content=audio,
        )
    text = r.json()["results"]["channels"][0]["alternatives"][0]["transcript"]
    return {"text": text}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8101)
```

Point config at it (`stt: {backend: custom, url: http://localhost:8101}`),
restart the portal — done.

## Emotion-tag walkthrough (the Zonos example)

1. Run the bundled shim with an emotion-capable engine: `tts.options.backend: zonos-transformer`.
2. The shim's `/capabilities` reports `emotion_control: true` and a
   `tool_prompt` describing the `instructions` field and any inline tags.
3. On MCP-server start, that prompt lands in the `say` tooldef; on session
   creation, in the voice role.
4. The agent emits `say(text="[chuckle] that took a while")` — the envelope
   carries it untouched, the engine renders an actual chuckle.
5. Flip back to `tts.backend: default` and the same call speaks "that took a
   while" — tags stripped, never read aloud. Graceful degradation is part of
   the contract.
