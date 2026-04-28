# Self-Hosted TTS Setup

> Living document. Update this, don't create new versions.

AgentWire supports multiple self-hosted TTS backends, each with different capabilities and hardware requirements. All backends share the same server (`agentwire tts start`) and can be hot-swapped at runtime without restarting.

> **Alternative:** [RunPod serverless TTS](runpod-tts.md) requires no GPU hardware and scales to zero when idle.

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
| `qwen-base-0.6b` | Qwen3-TTS 0.6B | ~4 GB | Yes | No | No | 10 languages | Yes |
| `qwen-base-1.7b` | Qwen3-TTS 1.7B | ~8 GB | Yes | No | No | 10 languages | Yes |
| `qwen-custom` | Qwen3-TTS CustomVoice | ~8 GB | Yes | Yes (instruct) | No | 10 languages | Yes |
| `qwen-design` | Qwen3-TTS VoiceDesign | ~8 GB | From text desc | Yes (instruct) | No | 10 languages | Yes |
| `zonos-transformer` | Zonos v0.1 Transformer | ~4 GB | Yes | Yes (7 sliders) | No | 5 languages | No |
| `zonos-hybrid` | Zonos v0.1 Hybrid (SSM) | ~4 GB | Yes | Yes (7 sliders) | No | 5 languages | No |

### Choosing a Backend

- **No GPU / CPU only** → `kokoro` (170 MB, near real-time, 30+ voices — use `af_heart` voice for best quality)
- **Best voice quality + emotion control** → `zonos-transformer`
- **Mid-sentence sounds** (laugh, sigh, cough) → `chatterbox` or `chatterbox-streaming`
- **Multilingual** (10 languages) → `qwen-base-1.7b` or `qwen-custom`
- **Generate a voice from a text description** → `qwen-design`
- **Low VRAM / fast** → `qwen-base-0.6b` or `zonos-transformer`

## Venv Setup

Each backend family runs in its own Python venv to avoid dependency conflicts.

| Venv | Backend Family |
|------|---------------|
| `.venv-kokoro` | `kokoro` |
| `.venv-chatterbox` | `chatterbox`, `chatterbox-streaming` |
| `.venv-qwen` | `qwen-base-0.6b`, `qwen-base-1.7b`, `qwen-custom`, `qwen-design` |
| `.venv-zonos` | `zonos-transformer`, `zonos-hybrid` |

`agentwire tts start` automatically selects the correct venv for the requested backend. If the venv doesn't exist, it will error with instructions.

### Creating the Kokoro venv

CPU-only. Uses CPU-only PyTorch (much smaller than CUDA builds — ~250 MB vs 2 GB+). The ONNX model (~170 MB) is auto-downloaded from HuggingFace on first use.

```bash
cd ~/projects/agentwire-dev
uv venv .venv-kokoro
source .venv-kokoro/bin/activate
pip install kokoro-onnx
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
pip install fastapi uvicorn faster-whisper pydantic python-multipart
pip install -e /path/to/agentwire-dev  # installs agentwire + remaining deps
```

### Creating the Chatterbox venv

```bash
cd ~/projects/agentwire-dev
uv venv .venv-chatterbox
source .venv-chatterbox/bin/activate
pip install chatterbox-tts torch torchaudio fastapi uvicorn faster-whisper pydantic
```

### Creating the Qwen venv

```bash
cd ~/projects/agentwire-dev
uv venv .venv-qwen
source .venv-qwen/bin/activate
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu124
pip install qwen-tts fastapi uvicorn faster-whisper pydantic
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
  backend: "zonos-transformer"  # runpod | kokoro | chatterbox | chatterbox-streaming | qwen-base-0.6b | qwen-base-1.7b | qwen-custom | qwen-design | zonos-transformer | zonos-hybrid
  url: "http://localhost:8100"
  default_voice: "default"

  # Chatterbox-specific (ignored by other backends)
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
