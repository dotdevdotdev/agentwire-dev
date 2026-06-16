# Self-Hosted STT (Speech-to-Text)

> Living document. Update this, don't create new versions.

AgentWire ships a local STT server (`agentwire stt start`) that the portal calls when you push-to-talk. The browser captures `audio/webm;codecs=opus`, the portal decodes it to 16 kHz mono PCM16 in-process via PyAV, then hands the WAV to the STT backend.

## Backends

| Backend | Model | Where it runs | Notes |
|---------|-------|---------------|-------|
| `moonshine` (default on Mac) | `moonshine/tiny` or `moonshine/base` (ONNX) | CPU | Fastest on Apple Silicon — no torch, no GPU. |
| `faster-whisper` | `tiny` → `large-v3` | CPU or CUDA | Higher accuracy, slower cold start. |
| `openai-whisper` | `tiny` → `large` | CPU or CUDA | Fallback when neither of the above is installed. |

Pick an engine via the config key `stt.engine: auto|moonshine|whisper` (default `auto` — moonshine first, whisper fallback). This is **orthogonal to `stt.backend`**, which is the portal *tier* (`default|cloud|custom`): the tier decides *where* transcription happens, the engine decides *which model* the self-hosted shim loads. So `stt: {backend: custom, engine: whisper}` boots the shim **and** runs faster-whisper. Equivalent overrides for ad-hoc use: `STT_BACKEND=moonshine|whisper` env or `agentwire stt start --backend ...`. Model name via `MOONSHINE_MODEL=...` or `WHISPER_MODEL=...`.

Don't want to run local models at all? That's not a shim concern — use the
portal's [`stt.backend: cloud` tier](stt-cloud.md) (hosted transcription API,
no shim process needed).

Backend selection logic lives in `agentwire/stt/engine.py` (FastAPI-free, unit-tested in `tests/unit/test_stt_engine.py`); `stt_server.py` is the HTTP wrapper.

## Quick Start

```bash
# 1. Install STT extras
uv pip install -e '.[stt]'
pip install useful-moonshine-onnx soundfile  # for moonshine

# 2. Start the server (defaults to moonshine/base on a Mac)
agentwire stt start

# 3. Point the portal at it
# ~/.agentwire/config.yaml
stt:
  url: "http://localhost:8101"
  timeout: 30
  silence_prepend_ms: 0   # default — see "Latency knobs" below
```

## Latency knobs

These two settings dominate felt push-to-talk latency. Inference itself (~350 ms for 3 s of audio on moonshine/tiny) is rarely the bottleneck.

### `stt.silence_prepend_ms` (default `0`)

Prepends a configurable amount of silence to the decoded WAV before sending it to the STT backend. Some older `faster-whisper` builds clip the first syllable when audio starts at t≈0. **Moonshine does not need this**, and prepending audio uniformly bumps inference time, so the default is `0`. Set to `~300` only if you observe first-syllable cutoffs on your backend.

### In-process WebM decoding (no knob)

The portal used to shell out to `ffmpeg -i in.webm out.wav` for every utterance. Subprocess cold-start was 100–300 ms *before* any actual decoding. The portal now decodes WebM/Opus in-process via [PyAV](https://pyav.org/) (libav bindings — no subprocess, no startup cost) and resamples to 16 kHz mono PCM16 in one pass. PyAV is a hard dependency of the portal — see `pyproject.toml`.

## Benchmark — felt latency on `moonshine/tiny` (Mac, M-series, CPU)

Push-to-talk a 3-second utterance and measure: button release → text appears in the input.

### Direct POST to STT server (no portal in the path)

| Run | Total ms | Server `transcribe_time` |
|---|---|---|
| 1 | 351 | 0.35 s |
| 2 | 441 | 0.44 s |
| 3 | 329 | 0.33 s |
| 4 | 338 | 0.34 s |
| 5 | 328 | 0.33 s |

p50 ≈ **340 ms**. Floor set by model inference.

### Decode step alone — old `ffmpeg` subprocess vs new in-process PyAV

Mac, M-series, 2 s `audio/webm;codecs=opus` (≈19 kB):

| | Run 1 | Run 2 | Run 3 | Run 4 | Run 5 |
|---|---|---|---|---|---|
| `ffmpeg -i in.webm out.wav` subprocess | 449 ms | 88 ms | 104 ms | 184 ms | 98 ms |
| PyAV in-process decode + WAV pack | 6.9 ms | 6.1 ms | 4.6 ms | 6.0 ms | 4.4 ms |

The first ffmpeg run pays the ~350 ms PATH/codec/cold-cache penalty; even warm, every utterance still eats ~90–180 ms of subprocess startup before any actual decoding. PyAV reuses already-loaded libav inside the portal process: ~5 ms per call, ~20–90× faster.

### Portal `/transcribe` — felt latency before / after

| | p50 (3 s utterance, push-to-talk) |
|---|---|
| Old path (ffmpeg + 300 ms silence prepend) | ~1.5–2 s |
| New path (PyAV + `silence_prepend_ms=0`) | ~400–500 ms |

The remaining headroom above the ~340 ms direct-POST floor is the multipart upload + temp-file write to hand off to the STT backend (which takes a `Path`).

Reproduce with:

```bash
# Direct POST (server only)
ffmpeg -f lavfi -i "sine=frequency=440:duration=3" -ar 16000 -ac 1 -y /tmp/tone.wav -loglevel error
for i in 1 2 3 4 5; do
  /usr/bin/time -p curl -s -F "file=@/tmp/tone.wav" http://localhost:8101/transcribe > /dev/null
done

# Portal end-to-end (simulate a browser upload)
ffmpeg -f lavfi -i "sine=frequency=440:duration=3" -c:a libopus -y /tmp/tone.webm -loglevel error
for i in 1 2 3 4 5; do
  /usr/bin/time -p curl -s -F "audio=@/tmp/tone.webm" http://localhost:8765/transcribe > /dev/null
done
```

## Sidebar: CoreML execution provider is a net loss on moonshine_onnx

Tried — load time 25.4 s → 3.2 s (real win), per-call inference ~457 ms → ~1342 ms (≈3× slower). Only 389 / 743 graph nodes are CoreML-compatible, so the autoregressive decoder bounces ANE↔CPU per token. Not worth pursuing until the upstream moonshine_onnx graph becomes more CoreML-friendly.

## Troubleshooting: "STT does nothing" (the silent double-fallback)

This bit us once and is worth recognizing fast, because it fails **silently in two
places**. Symptom: push-to-talk produces no transcription and no visible error
(often first noticed on a tablet/phone, which red-herrings you toward the client).

**The failure chain:**

1. The STT server (`agentwire-stt` tmux session on `:8101`) **crashes on startup** —
   most commonly `ModuleNotFoundError: No module named 'fastapi'` (or `moonshine_onnx`)
   because the project `.venv` is missing the STT deps. The crash is only visible in
   the tmux pane; the session stays alive at a shell prompt, so it *looks* started.
2. The portal builds `self.stt` once at startup via `get_stt_backend()`. With
   `stt.backend: custom`, every `/transcribe` then fails at request time with an
   STT-server error (there is no silent fallback — WhisperKit was removed with
   the two-tier model).

**Diagnose (in order):**

```bash
agentwire doctor                                   # now prints [!!] Stt if :8101 is down
curl -s http://localhost:8101/health               # {"status":"ok","model":{"backend":"moonshine",...}}
tmux capture-pane -pt agentwire-stt -S -50         # shows the real crash (missing import)
tmux capture-pane -pt agentwire-portal -S -200 | grep -i stt   # "Using STT shim" + backend name
```

The portal log line at startup is the tell:
- `Using STT shim at http://localhost:8101` + `STT backend: STTServerBackend` → good.
- `STT backend: default (browser speech recognition)` → custom tier isn't configured.

**Fix:** install the STT deps into the venv the server actually launches with
(`.venv/bin/python`), restart the server, then **restart the portal** (it only picks
the backend at startup):

```bash
uv pip install --python .venv/bin/python -e '.[stt]'   # declares moonshine + soundfile + fastapi
agentwire stt stop && agentwire stt start              # wait for "Moonshine ONNX loaded"
agentwire portal restart --dev                         # re-runs get_stt_backend()
```

**Why it stayed hidden historically:** an old WhisperKit fallback swallowed the
failure until request time. The two-tier model removed it — custom tier now fails
loudly at request time, and `agentwire doctor` health-checks `:8101` directly.

**Tablet note:** if the mic does nothing on a phone/tablet *before* audio ever reaches
`/transcribe`, that's a different problem — a non-secure browser context. Reaching the
portal at `https://<LAN-IP>:8765` with a click-through self-signed cert is **not** a
secure context, so `navigator.mediaDevices` is `undefined` and recording silently
no-ops. Give the tablet a trusted origin (tunnel/valid cert) or, to test, allow the
origin via `chrome://flags/#unsafely-treat-insecure-origin-as-secure`.

## Reference

- Portal endpoint: `POST /transcribe` — multipart `file=@audio.webm`. Returns `{"text": "..."}`.
- STT server endpoint: `POST /transcribe` on port 8101 — accepts WAV, MP3, M4A, WEBM (any format libav can decode).
- Source: `agentwire/server.py::handle_transcribe`, `agentwire/server.py::_decode_audio_to_wav`, `agentwire/stt/stt_server.py`.
- Config: `agentwire/config.py::STTConfig`.
