# Self-Hosted TTS Setup

> Living document. Update this, don't create new versions.

The bundled TTS server is the **reference implementation of the [shim contract](shim-contract.md)** — multiple engines, each with different capabilities and hardware requirements, all behind one server (`agentwire tts start`) with runtime hot-swap.

> **You don't need this page for a good voice.** `tts.backend: default` already speaks via **Kokoro-82M** — bundled with the base install, CPU-only, auto-downloaded (~200 MB) on first portal start (`agentwire tts warm` pre-downloads it). Since #398 the default tier runs Kokoro in a **portal-managed shim subprocess** (tmux `agentwire-kokoro`, `:8102`) rather than in-process — process isolation keeps the GIL-holding ONNX warm-up off the portal event loop, mirroring the STT shim (`:8101`). The portal auto-spawns it on startup; `agentwire kokoro start|stop|status` manage it by hand, and browser speechSynthesis covers speech until its `/health` reports `ok`. This page is the `custom` tier: voice cloning, GPU engines, emotion control, or any other model behind the shim contract.

## Quick Start

```bash
# Start with default backend (chatterbox)
agentwire tts start

# Start with a specific backend
agentwire tts start --backend zonos-transformer

# Test it
agentwire say "Hello, this is a test"

# Hot-swap backend at runtime (no restart needed)
curl -X POST http://localhost:8100/engines/zonos-transformer/load
```

## Backends

| Backend | Model | VRAM | Voice Cloning | Emotion Control | Paralinguistic Tags | Languages | Streaming |
|---------|-------|------|---------------|-----------------|--------------------|-----------|----|
| `kokoro` | Kokoro 82M ONNX | **CPU only** | No | No | No | 8 languages | Yes |
| `chatterbox` | Chatterbox Turbo (350M) | ~4–8 GB | Yes | No | Yes (`[laugh]` etc.) | English | No |
| `chatterbox-streaming` | Chatterbox Streaming | ~4–8 GB | Yes | No | Yes | English | Yes |
| `zonos-transformer` | Zonos v0.1 Transformer | ~4 GB | Yes | Yes (7 sliders) | No | 5 languages | No |
| `zonos-hybrid` | Zonos v0.1 Hybrid (SSM) | ~4 GB | Yes | Yes (7 sliders) | No | 5 languages | No |

### Choosing a Backend

- **No GPU / CPU only** → you likely don't need this server at all: the `default` tier already runs kokoro in its own portal-managed shim (`agentwire-kokoro`, `:8102`). Run `kokoro` behind *this* multi-engine shim only when serving TTS to other machines or hot-swapping engines.
- **Best voice quality + emotion control** → `zonos-transformer`
- **Mid-sentence sounds** (laugh, sigh, cough) → `chatterbox` or `chatterbox-streaming`
- **Multilingual** (5 languages) → `zonos-transformer` or `zonos-hybrid`
- **Low VRAM / fast** → `zonos-transformer`

## Venv Setup

Each backend family runs in its own Python venv to avoid dependency conflicts.

| Venv | Backend Family |
|------|---------------|
| `.venv-kokoro` | `kokoro` |
| `.venv-chatterbox` | `chatterbox`, `chatterbox-streaming` |
| `.venv-zonos` | `zonos-transformer`, `zonos-hybrid` |

`agentwire tts start` automatically selects the correct venv for the requested backend. If the venv doesn't exist, it will error with instructions.

### Creating the Kokoro venv

CPU-only, torch-free (the engine is pure ONNX). The model (~170 MB) is auto-downloaded on first use to `~/.cache/kokoro_onnx/`.

```bash
cd ~/projects/agentwire-dev
uv venv .venv-kokoro
source .venv-kokoro/bin/activate
pip install kokoro-onnx fastapi uvicorn faster-whisper pydantic python-multipart
pip install -e /path/to/agentwire-dev  # installs agentwire + remaining deps
```

### Creating the Chatterbox venv

**Requires Python 3.13+** — chatterbox-tts pins `numpy<2` on older Pythons, which conflicts with the base install's kokoro-onnx (`numpy>=2`).

```bash
cd ~/projects/agentwire-dev
uv venv .venv-chatterbox --python 3.13
source .venv-chatterbox/bin/activate
pip install chatterbox-tts torch torchaudio fastapi uvicorn faster-whisper pydantic
```

### Creating the Zonos venv

Zonos must be installed in **editable mode** from a local clone due to a packaging bug (`backbone/` sub-package is omitted by the standard pip install):

```bash
# System dep (required for phonemization)
sudo apt-get install -y espeak-ng

cd ~/projects/agentwire-dev
uv venv .venv-zonos
.venv-zonos/bin/python -m ensurepip
.venv-zonos/bin/python -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu124
git clone --depth 1 https://github.com/Zyphra/Zonos.git /tmp/Zonos
.venv-zonos/bin/python -m pip install -e /tmp/Zonos
.venv-zonos/bin/python -m pip install fastapi uvicorn faster-whisper pydantic
```

**Hybrid model note:** `zonos-hybrid` additionally requires `mamba-ssm` and `causal-conv1d`, which need CUDA toolkit (nvcc) to compile. Use `zonos-transformer` if you don't need the SSM architecture — quality is identical.

```bash
# Optional: enable zonos-hybrid
sudo apt-get install -y cuda-nvcc-12-4 cuda-compiler-12-4 cuda-cudart-dev-12-4
export PATH=/usr/local/cuda-12.4/bin:$PATH
.venv-zonos/bin/python -m pip install mamba-ssm causal-conv1d --no-build-isolation
```

## Configuration

```yaml
tts:
  backend: "custom"
  url: "http://localhost:8100"
  default_voice: "default"
  options:
    backend: zonos-transformer  # engine: kokoro | chatterbox | chatterbox-streaming
                                # | zonos-transformer | zonos-hybrid
  # Chatterbox-style knobs (ignored by other engines)
  exaggeration: 0.5
  cfg_weight: 0.5
```

## Emotion Control (Zonos)

Zonos supports 7 independent emotion sliders. All default to `0.0`; `neutral` auto-fills the remainder.

```bash
# Via API
curl -X POST http://localhost:8100/tts \
  -H "Content-Type: application/json" \
  -d '{"text": "I cannot believe this!", "voice": "default", "emotion_happiness": 0.9}'

# Pure fear
-d '{"text": "Something is out there...", "emotion_fear": 1.0}'

# Mixed (nervous excitement)
-d '{"text": "Here we go!", "emotion_happiness": 0.6, "emotion_fear": 0.3}'
```

| Parameter | Range | Effect |
|-----------|-------|--------|
| `emotion_happiness` | 0.0–1.0 | Joy, excitement |
| `emotion_sadness` | 0.0–1.0 | Grief, resignation |
| `emotion_disgust` | 0.0–1.0 | Revulsion |
| `emotion_fear` | 0.0–1.0 | Fear, anxiety |
| `emotion_surprise` | 0.0–1.0 | Shock, wonder |
| `emotion_anger` | 0.0–1.0 | Frustration, rage |
| `emotion_other` | 0.0–1.0 | Miscellaneous expressive |
| `speaking_rate` | float | Tokens/sec (default ~15.0) |
| `pitch_std` | float | Pitch variation (default ~45.0) |

## Paralinguistic Tags (Chatterbox only)

Chatterbox Turbo supports inline sound tags:

```bash
agentwire say "[laugh] That actually worked!"
agentwire say "[sigh] Alright, let me try a different approach"
agentwire say "[gasp] I had no idea"
```

| Tag | Effect |
|-----|--------|
| `[laugh]` | Laughter |
| `[chuckle]` | Light amusement |
| `[cough]` | Cough |
| `[sigh]` | Sigh |
| `[gasp]` | Surprise gasp |

## Voices

```bash
# List available voices
curl http://localhost:8100/voices

# Upload a new voice (10–30s WAV recommended)
curl -X POST http://localhost:8100/voices/myvoice -F "file=@sample.wav"

# Delete a voice
curl -X DELETE http://localhost:8100/voices/myvoice

# Use a voice
agentwire say --voice myvoice "Hello"
```

Voice files live in `~/.agentwire/voices/`. The `default` voice is used when no `--voice` flag is provided.

## Hot-Swap Backends

Switch backends at runtime without restarting the server:

```bash
# Via CLI
curl -X POST http://localhost:8100/engines/zonos-transformer/load

# Check current engine
curl http://localhost:8100/health

# List all registered engines
curl http://localhost:8100/engines
```

Only one engine is loaded at a time. Switching unloads the previous one and clears GPU cache automatically.

## CLI Commands

```bash
agentwire tts start                           # Start with default backend
agentwire tts start --backend zonos-transformer  # Start with specific backend
agentwire tts stop                            # Stop the server
agentwire tts status                          # Check status and current engine
agentwire tts restart                         # Restart (picks up config changes)
```

## Smart Audio Routing

`agentwire say` automatically routes audio:

1. **Browser connected** → streams to browser (tablet/phone/laptop)
2. **No browser** → plays on local speakers

```bash
agentwire say "Task complete"                 # auto-routes
agentwire say --voice shoe "How does this sound?"
agentwire say -s myproject "Message for that session"
```
