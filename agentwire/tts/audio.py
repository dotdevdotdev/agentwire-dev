"""Torch-free audio serialization helpers shared by engines, the bundled
TTS server, and the portal's in-process Kokoro path."""

import io
import wave

import numpy as np


def pcm_float_to_wav_bytes(samples: np.ndarray, sample_rate: int) -> bytes:
    """Serialize float32 PCM samples (mono, any shape squeezable to 1-D,
    range [-1, 1]) to 16-bit WAV bytes using only the stdlib wave module."""
    pcm = np.asarray(samples, dtype=np.float32).squeeze()
    pcm_int16 = (pcm * 32767).clip(-32768, 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_int16.tobytes())
    buf.seek(0)
    return buf.read()
