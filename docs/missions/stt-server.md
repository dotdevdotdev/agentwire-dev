> Living document. Update this, don't create new versions.

# Mission: Persistent STT Server

**Issue:** [#65](https://github.com/dotdevdotdev/agentwire/issues/65)
**Branch:** `65-stt-server`
**Status:** In Progress

## Goal

Eliminate STT cold start delays by implementing a persistent server that keeps the WhisperKit model loaded in memory.

## Problem

- Current: Each transcription spawns `whisperkit-cli` → 3-5+ second model load
- `agentwire stt start/stop/status` commands exist but `stt_server.py` is missing
- Result: Poor UX on first use or after idle periods

## Solution

Create `agentwire/stt/stt_server.py` following the TTS server pattern:

```
agentwire/
├── stt/
│   ├── __init__.py
│   └── stt_server.py    # FastAPI app
└── listen.py            # Update to use server
```

### Server API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/transcribe` | POST | Accept WAV audio, return `{"text": "..."}` |
| `/health` | GET | Health check for status commands |

### Key Design Decisions

1. **Local-first** - STT works best on local machine (no audio upload latency)
2. **Fallback to CLI** - If server not running, use current `whisperkit-cli` behavior
3. **Model selection** - Support tiny/base/small/medium/large-v3 via config

## Tasks

- [x] Create `agentwire/stt/__init__.py`
- [x] Create `agentwire/stt/stt_server.py` with FastAPI app
- [x] Load Whisper model at startup (faster-whisper with openai-whisper fallback)
- [x] Implement `/transcribe` endpoint
- [x] Implement `/health` endpoint
- [x] Update `listen.py` to check for server and use it
- [ ] Test `agentwire stt start/stop/status` commands
- [ ] Update CLAUDE.md config docs

## Reference

- TTS server: `agentwire/tts_server.py`
- Listen module: `agentwire/listen.py`
- CLI commands: `agentwire/__main__.py` (search for `stt`)

## Notes

### Port Assignment

- TTS server: port 8100 (remote GPU machine, tunneled to local)
- STT server: port 8101 (local CPU, best latency)

### Backend Selection

The STT server uses `faster-whisper` (CTranslate2) for better performance, with fallback to `openai-whisper` if unavailable. Unlike the local `whisperkit-cli` (CoreML/Apple Neural Engine), this uses standard Whisper models that work on any machine.

### listen.py Flow

1. Check if STT server is healthy at `stt.url` (default: `http://localhost:8101`)
2. If yes: POST audio → instant transcription (~0.3-0.5s)
3. If no: Fall back to `whisperkit-cli` (3-5s cold start)
