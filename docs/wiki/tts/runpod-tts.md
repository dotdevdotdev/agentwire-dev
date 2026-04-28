# RunPod Serverless TTS

AgentWire TTS can run on RunPod serverless infrastructure. This provides pay-per-use GPU access without maintaining dedicated hardware.

## Why RunPod?

| Benefit | Description |
|---------|-------------|
| **No GPU required** | Run TTS on cloud GPUs |
| **Pay per use** | Only charged when generating audio |
| **Auto-scaling** | Scales to zero when idle, spins up on demand |
| **Custom voices** | Upload voice clones to network volume |

## Setup

### 1. Create RunPod Account

Sign up at [runpod.io](https://runpod.io) and get your API key from Settings.

### 2. Create Serverless Endpoint

1. Go to **Serverless** → **Endpoints** → **New Endpoint**
2. Use this Docker image: `dotdevdotdev/agentwire-tts:latest`
3. Configure:
   - **Workers**: Min 0, Max 1
   - **Idle Timeout**: 180 seconds
   - **Execution Timeout**: 180 seconds
   - **GPU**: Any CUDA-compatible (RTX 3090, A100, etc.)
4. (Optional) Attach a **Network Volume** for custom voices

### 3. Configure AgentWire

Edit `~/.agentwire/config.yaml`:

```yaml
tts:
  backend: runpod
  default_voice: default
  runpod_endpoint_id: your_endpoint_id
  runpod_api_key: your_runpod_api_key
```

Or use environment variables:

```bash
export AGENTWIRE_TTS__RUNPOD_ENDPOINT_ID=your_endpoint_id
export AGENTWIRE_TTS__RUNPOD_API_KEY=your_api_key
```

### 4. Test

```bash
agentwire say "Hello from RunPod"
```

## Custom Voices

Upload voice clones to your RunPod network volume:

```python
import base64
import requests
from pathlib import Path

# Read your voice sample
voice_path = Path("~/.agentwire/voices/myvoice.wav").expanduser()
audio_b64 = base64.b64encode(voice_path.read_bytes()).decode('utf-8')

# Upload to network volume
response = requests.post(
    f"https://api.runpod.ai/v2/{endpoint_id}/runsync",
    json={
        "input": {
            "action": "upload_voice",
            "voice_name": "myvoice",
            "audio_base64": audio_b64
        }
    },
    headers={"Authorization": f"Bearer {api_key}"},
    timeout=300
)
```

Then use it:

```bash
agentwire say "Hello" -v myvoice
```

## Troubleshooting

**Timeout errors:**
- Cold starts take 10-20 seconds on first request
- Increase timeout in config if needed

**Voice not found:**
- Verify voice was uploaded to network volume
- Check voice name matches exactly
