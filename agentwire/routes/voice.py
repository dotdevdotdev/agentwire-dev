"""Portal routes — voice domain (thin TTS/STT HTTP endpoints).

Part of the #560 server.py split. Handlers moved verbatim from
``AgentWireServer``; they depend on core attributes and the TTS/STT engine
helpers (``self.speak``, ``self._probe_shim``, ``self._tts_*``, ``self.stt``,
``self._get_voices``, …), which stay on the base server and resolve through the
MRO of the composed server class. Only the thin HTTP endpoints live here — the
engine itself does not move.

``_decode_audio_to_wav`` is a private static helper used solely by
``handle_transcribe`` and so travels with it.
"""

import asyncio
import logging
import tempfile
import time
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)


class VoiceRoutesMixin:
    async def api_voice_status(self, request: web.Request) -> web.Response:
        """GET /api/voice-status — voice tier + availability for the frontend.

        The portal uses this to pick its input/output paths (browser speech
        vs audio upload) and to render the instant-mode banner. Custom-shim
        probes are cached for 30s.
        """
        now = time.time()
        cached = getattr(self, "_voice_status_cache", None)
        if cached and now - cached[0] < 30:
            return web.json_response(cached[1])

        stt_cfg, tts_cfg = self.config.stt, self.config.tts

        stt: dict = {"backend": stt_cfg.backend, "url": stt_cfg.url, "available": True}
        # server_transcribe drives the frontend's browser-vs-upload choice: true
        # → MediaRecorder POST /transcribe, false → browser SpeechRecognition.
        stt["server_transcribe"] = stt_cfg.backend in ("cloud", "custom")
        if stt_cfg.backend == "custom":
            stt["available"] = await self._probe_shim(stt_cfg.url, "/health") is not None
        elif stt_cfg.backend == "default":
            # Portal-managed Moonshine shim subprocess. The client only uploads
            # once the shim's /health is "ok" (model loaded); while it loads or
            # if the spawn failed, server_transcribe stays false and the client
            # keeps using browser speech recognition. available stays true —
            # browser fallback is always there.
            from ..stt import _default_stt_url

            health = await self._probe_shim(_default_stt_url(stt_cfg), "/health")
            stt["server_transcribe"] = bool(health and health.get("status") == "ok")

        tts: dict = {"backend": tts_cfg.backend, "url": tts_cfg.url, "available": True}
        if tts_cfg.backend == "default":
            # Portal-managed Kokoro shim subprocess. Probe its /health for the
            # warm-up state (mirrors the STT shim); the browser keeps
            # synthesizing speech until status is "ok", and `available` stays
            # true because that browser fallback is always there.
            from ..tts import _default_tts_url

            health = await self._probe_shim(_default_tts_url(tts_cfg), "/health")
            state = health.get("status") if health else "absent"
            percent = health.get("percent", 0) if health else 0
            tts["kokoro"] = {"state": state, "percent": percent}
            if health and health.get("error"):
                tts["kokoro"]["error"] = health["error"]
            if state == "ok":
                from ..tts.engines.kokoro import PRESET_VOICES

                tts["voices"] = list(PRESET_VOICES)
        elif tts_cfg.backend == "custom":
            health = await self._probe_shim(tts_cfg.url, "/health")
            tts["available"] = health is not None
            if tts["available"]:
                caps = await self._probe_shim(tts_cfg.url, "/capabilities")
                if caps:
                    if caps.get("tool_prompt"):
                        tts["tool_prompt"] = caps["tool_prompt"]
                    if caps.get("voices") is not None:
                        tts["voices"] = caps["voices"]

        status = {
            "stt": stt,
            "tts": tts,
            "corrections": stt_cfg.corrections,
            # Instant (zero-round-trip browser) mode only holds while STT stays
            # browser-side; once host Moonshine takes over, audio uploads.
            "instant_mode": not stt["server_transcribe"] and tts_cfg.backend == "default",
        }
        self._voice_status_cache = (now, status)
        return web.json_response(status)

    async def api_voices(self, request: web.Request) -> web.Response:
        """Get available TTS voices."""
        voices = await self._get_voices()
        return web.json_response(voices)

    async def handle_transcribe(self, request: web.Request) -> web.Response:
        """Transcribe audio to text.

        Decodes WebM/Opus uploads in-process via PyAV (no ffmpeg subprocess
        startup) and resamples to 16 kHz mono PCM16 — the canonical input
        shape for Whisper- and Moonshine-class models. Optionally prepends a
        configurable amount of silence (``stt.silence_prepend_ms``, default 0).

        All three tiers transcribe server-side: default and custom via an HTTP
        shim, cloud via a hosted API. If the default-tier shim isn't ready yet
        the backend raises and this endpoint answers 500 (the client is already
        using browser speech recognition until /api/voice-status flips).
        """
        try:
            reader = await request.multipart()
            audio_field = await reader.next()

            if audio_field is None:
                return web.json_response({"error": "No audio data"})

            audio_data = await audio_field.read()
            if not audio_data:
                return web.json_response({"error": "Empty audio data"})

            silence_ms = int(getattr(self.config.stt, "silence_prepend_ms", 0) or 0)

            try:
                wav_data = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self._decode_audio_to_wav,
                    audio_data,
                    silence_ms,
                )
            except Exception as e:
                logger.error("Failed to decode audio: %s", e)
                return web.json_response({"error": "Audio conversion failed"})

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(wav_data)
                wav_path = f.name

            try:
                logger.info("Transcribing %s via %s backend", wav_path, type(self.stt).__name__)
                text = await self.stt.transcribe(Path(wav_path))
                logger.info("Transcription result: %s", text)
                return web.json_response({"text": text})
            finally:
                Path(wav_path).unlink(missing_ok=True)

        except Exception as e:
            logger.error(f"Transcription failed: {e}")
            return web.json_response({"error": str(e)})

    @staticmethod
    def _decode_audio_to_wav(audio_data: bytes, silence_prepend_ms: int = 0) -> bytes:
        """Decode arbitrary input audio (WebM/Opus, MP3, M4A, …) to 16 kHz mono PCM16 WAV.

        Replaces the previous ``ffmpeg -i in.webm out.wav`` subprocess. Subprocess
        cold-start was 100–300 ms before any actual decoding; PyAV uses libav
        bindings in-process so the only cost is the decoding itself.
        """
        import io
        import wave

        import av  # PyAV — declared in pyproject.toml `dependencies`

        target_rate = 16000

        with av.open(io.BytesIO(audio_data), mode="r") as container:
            if not container.streams.audio:
                raise RuntimeError("Input contains no audio stream")

            resampler = av.AudioResampler(format="s16", layout="mono", rate=target_rate)
            pcm_chunks: list[bytes] = []

            def _frame_bytes(f) -> bytes:
                # AudioFrame.planes[0] may include SIMD alignment padding; slice
                # to the exact PCM length (samples × channels × bytes_per_sample).
                bytes_per_sample = f.format.bytes  # 2 for s16
                channels = len(f.layout.channels)  # 1 for mono
                size = f.samples * channels * bytes_per_sample
                return bytes(f.planes[0])[:size]

            for frame in container.decode(audio=0):
                for resampled in resampler.resample(frame):
                    pcm_chunks.append(_frame_bytes(resampled))
            for resampled in resampler.resample(None):
                pcm_chunks.append(_frame_bytes(resampled))

        if silence_prepend_ms > 0:
            silence_samples = int(target_rate * silence_prepend_ms / 1000)
            pcm_chunks.insert(0, b"\x00\x00" * silence_samples)

        pcm_data = b"".join(pcm_chunks)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)  # 16-bit
            wav.setframerate(target_rate)
            wav.writeframes(pcm_data)
        return buf.getvalue()

    async def handle_send(self, request: web.Request) -> web.Response:
        """Send text to an agent session via CLI."""
        name = request.match_info["name"]
        try:
            data = await request.json()
            text = data.get("text", "").strip()

            if not text:
                return web.json_response({"error": "No text provided"})

            # Notify dashboard that session is now processing (for agentwire indicator)
            await self.broadcast_dashboard("session_processing", {"session": name, "processing": True})

            # Use CLI: agentwire send -s <session> <text>
            success, result = await self.run_agentwire_cmd(["send", "-s", name, text])

            if not success:
                error_msg = result.get("error", "Failed to send to session")
                return web.json_response({"error": error_msg})

            return web.json_response({"success": True})

        except Exception as e:
            logger.error(f"Send failed: {e}")
            return web.json_response({"error": str(e)})

    async def api_say(self, request: web.Request) -> web.Response:
        """POST /api/say/{session} - Generate TTS and broadcast to session."""
        name = request.match_info["name"]
        try:
            data = await request.json()
            text = data.get("text", "").strip()

            if not text:
                return web.json_response({"error": "No text provided"}, status=400)

            # Ensure session exists (create if not)
            if name not in self.active_sessions:
                from ..server import Session

                self.active_sessions[name] = Session(name=name, config=self._get_session_config(name))

            session = self.active_sessions[name]

            # Track this text to avoid duplicate TTS from output polling
            session.played_says.add(text)
            if len(session.played_says) > 50:
                session.played_says = set(list(session.played_says)[-25:])

            # Count chunks for the response (speak() does the actual chunking)
            from ..utils.chunker import chunk_text
            chunks = chunk_text(text)
            chunk_count = len(chunks)

            logger.info(f"[{name}] API say: {text[:50]}... ({chunk_count} chunk(s))")

            # Generate and broadcast TTS in background (don't block the API response)
            # speak() handles chunking sequentially — guaranteed playback order
            asyncio.create_task(self.speak(name, text))

            return web.json_response({"success": True, "chunks": chunk_count})

        except Exception as e:
            logger.error(f"Say API failed: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def api_local_tts(self, request: web.Request) -> web.Response:
        """POST /api/local-tts/{session} - Generate TTS and return audio for local playback."""
        name = request.match_info["name"]
        try:
            data = await request.json()
            text = data.get("text", "").strip()
            voice = data.get("voice")

            if not text:
                return web.json_response({"error": "No text provided"}, status=400)

            # Default tier: Kokoro shim on local speakers when ready, OS voice
            # while it warms up (process-isolated — see ensure_managed_tts)
            if self.config.tts.backend == "default":
                from ..utils.speech import strip_speech_tags

                clean = strip_speech_tags(text)
                if await self._kokoro_shim_ready():
                    try:
                        session_config = self._get_session_config(name)
                        wav = await self._tts_generate(
                            clean, voice or session_config.voice
                        )
                        if wav and await self._play_wav_locally(wav):
                            return web.json_response(
                                {"success": True, "tier": "default", "engine": "kokoro"}
                            )
                    except Exception as e:
                        logger.error(f"Kokoro local TTS failed: {e}")
                ok = await self._os_say(clean)
                if ok:
                    return web.json_response({"success": True, "tier": "default"})
                return web.json_response(
                    {"success": False, "error": "OS voice playback failed"},
                    status=500,
                )

            # Get session config for defaults
            session_config = self._get_session_config(name)
            if voice is None:
                voice = session_config.voice
            exaggeration = session_config.exaggeration
            cfg_weight = session_config.cfg_weight

            logger.info(f"[{name}] Local TTS: {text[:50]}... (voice={voice})")

            # Generate audio via TTS shim HTTP call
            audio_data = await self._tts_generate(
                text=text,
                voice=voice,
                instructions=self.config.tts.instructions or None,
                options=self._tts_envelope_options(exaggeration, cfg_weight),
            )

            if not audio_data:
                return web.json_response(
                    {"success": False, "error": "TTS generation returned no audio"},
                    status=500
                )

            if await self._play_wav_locally(audio_data):
                return web.json_response({"success": True})
            return web.json_response(
                {"success": False, "error": "Local audio playback failed"},
                status=500,
            )

        except asyncio.TimeoutError:
            logger.error(f"TTS generation timeout for: {text[:50]}...")
            return web.json_response(
                {"success": False, "error": "TTS generation timeout"},
                status=500
            )
        except Exception as e:
            logger.error(f"Local TTS API failed: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def api_answer(self, request: web.Request) -> web.Response:
        """POST /api/answer/{session} - Answer an AskUserQuestion prompt."""
        name = request.match_info["name"]
        try:
            data = await request.json()
            answer = data.get("answer", "").strip()
            is_custom = data.get("custom", False)
            option_number = data.get("option_number")  # For "type something" flow

            if not answer:
                return web.json_response({"error": "No answer provided"}, status=400)

            # Three modes:
            # 1. Regular option: just send the number key (no Enter)
            # 2. "Type something" option: send number key, wait, send text + Enter
            # 3. Direct custom: just send text + Enter (free-form input without numbered option)
            if option_number:
                # "Type something" flow: select option first (no Enter), then type
                self.agent.send_keys(name, str(option_number))
                await asyncio.sleep(0.5)  # Wait for Claude to show text input
                success = self.agent.send_input(name, answer)  # text + Enter
            elif is_custom:
                # Direct custom answer: type the text and press Enter
                success = self.agent.send_input(name, answer)
            else:
                # Just send the number key - AskUserQuestion responds to single keypress
                success = self.agent.send_keys(name, str(answer))

            if not success:
                return web.json_response({"error": "Failed to send answer"}, status=500)

            # Notify clients the question was answered
            if name in self.active_sessions:
                session = self.active_sessions[name]
                session.last_question = None
                await self._broadcast(session, {"type": "question_answered"})

            logger.info(f"[{name}] Answered: {answer}")
            return web.json_response({"success": True})

        except Exception as e:
            logger.error(f"Answer API failed: {e}")
            return web.json_response({"error": str(e)}, status=500)


def register_voice_routes(server, app):
    """Wire the voice domain's routes onto ``app``."""
    app.router.add_post("/transcribe", server.handle_transcribe)
    app.router.add_post("/send/{name:.+}", server.handle_send)
    app.router.add_post("/api/say/{name:.+}", server.api_say)
    app.router.add_post("/api/local-tts/{name:.+}", server.api_local_tts)
    app.router.add_post("/api/answer/{name:.+}", server.api_answer)
    app.router.add_get("/api/voices", server.api_voices)
    app.router.add_get("/api/voice-status", server.api_voice_status)
