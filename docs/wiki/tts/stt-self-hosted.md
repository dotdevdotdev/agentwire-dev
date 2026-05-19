# Self-Hosted STT (Speech-to-Text)

> Living document. Update this, don't create new versions.

AgentWire ships a local STT server (`agentwire stt start`) that the portal calls when you push-to-talk. The browser captures `audio/webm;codecs=opus`, the portal decodes it to 16 kHz mono PCM16 in-process via PyAV, then hands the WAV to the STT backend.

## Backends

| Backend | Model | Where it runs | Notes |
|---------|-------|---------------|-------|
| `moonshine` (default on Mac) | `moonshine/tiny` or `moonshine/base` (ONNX) | CPU | Fastest on Apple Silicon — no torch, no GPU. |
| `faster-whisper` | `tiny` → `large-v3` | CPU or CUDA | Higher accuracy, slower cold start. |
| `openai-whisper` | `tiny` → `large` | CPU or CUDA | Fallback when neither of the above is installed. |

Pick a backend via `STT_BACKEND=moonshine|whisper`. Model name via `MOONSHINE_MODEL=...` or `WHISPER_MODEL=...`.

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

## Reference

- Portal endpoint: `POST /transcribe` — multipart `file=@audio.webm`. Returns `{"text": "..."}`.
- STT server endpoint: `POST /transcribe` on port 8101 — accepts WAV, MP3, M4A, WEBM (any format libav can decode).
- Source: `agentwire/server.py::handle_transcribe`, `agentwire/server.py::_decode_audio_to_wav`, `agentwire/stt/stt_server.py`.
- Config: `agentwire/config.py::STTConfig`.
