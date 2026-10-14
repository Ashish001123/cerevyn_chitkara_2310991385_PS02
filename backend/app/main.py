"""FastAPI service: voice in -> STT -> dialogue manager -> TTS -> voice out.

Run with:  uvicorn app.main:create_app --factory
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .call import CallSession
from .config import ROOT, Settings, make_clock
from .db import seed_database
from .dialogue import DialogueManager
from .repository import Repository
from .speech import STTEngine, TTSEngine, build_speech, new_audio_path

log = logging.getLogger("voice_agent")

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ALLOWED_AUDIO_SUFFIXES = {".wav", ".webm", ".ogg", ".mp3", ".m4a", ".flac"}


class TextTurn(BaseModel):
    text: str
    speak: bool = True


def create_app(settings: Settings | None = None, stt: STTEngine | None = None,
               tts: TTSEngine | None = None) -> FastAPI:
    settings = settings or Settings()
    seed_database(settings.db_path, settings.seed_path)
    clock = make_clock(settings.app_now)
    repo = Repository(settings.db_path, clock)
    dialogue = DialogueManager(repo, clock)
    if stt is None or tts is None:
        default_stt, default_tts = build_speech(settings, repo.services())
        stt, tts = stt or default_stt, tts or default_tts

    tts_dir = settings.audio_out_dir
    upload_dir = settings.audio_out_dir.parent / "uploads"
    tts_dir.mkdir(parents=True, exist_ok=True)
    upload_dir.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="PS02 Voice Appointment Booking Agent", version="1.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.repo, app.state.dialogue, app.state.stt, app.state.tts = repo, dialogue, stt, tts
    app.state.tts_dir = tts_dir
    app.mount("/audio", StaticFiles(directory=tts_dir), name="audio")
    if hasattr(stt, "warm_up"):
        threading.Thread(target=stt.warm_up, daemon=True).start()

    def speak(text: str) -> str:
        return str(tts.synthesize(text, new_audio_path(tts_dir, "agent")))

    def audio_url(path: str | None) -> str | None:
        return f"/audio/{Path(path).name}" if path else None

    def require_session(session_id: str) -> None:
        if repo.load_state(session_id) is None:
            raise HTTPException(404, "session not found")

    frontend_dist = ROOT.parent / "frontend" / "dist"
    if (frontend_dist / "assets").exists():
        app.mount("/assets", StaticFiles(directory=frontend_dist / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    def index():
        if (frontend_dist / "index.html").exists():
            return FileResponse(frontend_dist / "index.html")
        return FileResponse(ROOT / "app" / "static" / "index.html")

    @app.get("/pcm-processor.js", include_in_schema=False)
    def pcm_worklet():
        worklet_path = ROOT.parent / "frontend" / "public" / "pcm-processor.js"
        if worklet_path.exists():
            return FileResponse(worklet_path, media_type="application/javascript")
        raise HTTPException(404, "Worklet not found")

    @app.get("/health")
    def health():
        return {"status": "ok", "stt": stt.name, "tts": tts.name}

    @app.get("/services")
    def services():
        return repo.services()

    @app.get("/slots")
    def slots(service: str | None = None, date: str | None = None):
        """Available slots only, read from the database."""
        return repo.available_slots(service, date)

    @app.post("/sessions")
    def start_session(speak_reply: bool = True):
        session_id, reply, state, audio = dialogue.start_session(speak if speak_reply else None)
        return {"session_id": session_id, "reply_text": reply, "reply_audio_url": audio_url(audio), "state": state}

    @app.post("/sessions/{session_id}/voice")
    def voice_turn(session_id: str, audio: UploadFile = File(...)):
        require_session(session_id)
        suffix = Path(audio.filename or "audio.wav").suffix.lower() or ".wav"
        if suffix not in ALLOWED_AUDIO_SUFFIXES:
            raise HTTPException(415, f"unsupported audio type {suffix}")
        upload_path = upload_dir / f"user-{uuid.uuid4().hex[:10]}{suffix}"
        with upload_path.open("wb") as f:
            shutil.copyfileobj(audio.file, f)
        if upload_path.stat().st_size > MAX_UPLOAD_BYTES:
            upload_path.unlink()
            raise HTTPException(413, "audio too large")
        t0 = time.perf_counter()
        transcript = stt.transcribe(upload_path)
        t1 = time.perf_counter()
        if not transcript:
            reply = "Sorry, I didn't catch that. Could you say it again?"
            reply_audio = speak(reply)
            state = repo.load_state(session_id)
        else:
            reply, state, reply_audio = dialogue.handle(session_id, transcript, user_audio=str(upload_path),
                                                        speak=speak)
        t2 = time.perf_counter()
        return {"transcript": transcript, "reply_text": reply, "reply_audio_url": audio_url(reply_audio),
                "state": state, "timings_ms": {"stt": round((t1 - t0) * 1000),
                                               "dialogue_and_tts": round((t2 - t1) * 1000)}}

    @app.post("/sessions/{session_id}/text")
    def text_turn(session_id: str, body: TextTurn):
        """Same pipeline without STT, for debugging and accessibility."""
        require_session(session_id)
        reply, state, reply_audio = dialogue.handle(session_id, body.text, speak=speak if body.speak else None)
        return {"transcript": body.text, "reply_text": reply, "reply_audio_url": audio_url(reply_audio),
                "state": state}

    @app.get("/sessions/{session_id}")
    def get_session(session_id: str):
        require_session(session_id)
        return {"session_id": session_id, "state": repo.load_state(session_id), "turns": repo.turns(session_id)}

    @app.get("/bookings")
    def list_bookings():
        return repo.list_bookings()

    @app.get("/bookings/{booking_id}")
    def get_booking(booking_id: str):
        booking = repo.get_booking(booking_id)
        if booking is None:
            raise HTTPException(404, "booking not found")
        return booking

    @app.get("/audit")
    def audit():
        return repo.audit_log()

    @app.websocket("/ws/calls")
    async def live_call(ws: WebSocket, session_id: str | None = None):
        """Full-duplex call: client streams 16 kHz PCM16 mic audio, server streams events and reply audio.

        Client -> server: binary audio frames; JSON {"type": "playback", "playing": bool} | {"type": "hangup"}.
        Server -> client: JSON events (session, user_speech_start, barge_in, transcript, agent_text,
        agent_audio [one WAV per sentence], timings, ignored).
        """
        await ws.accept()
        loop = asyncio.get_running_loop()
        if session_id is None or repo.load_state(session_id) is None:
            session_id, greeting, state, _ = dialogue.start_session()
        else:
            greeting, state = None, repo.load_state(session_id)
        call = CallSession(dialogue, stt, tts, session_id, upload_dir)
        await ws.send_json({"type": "session", "session_id": session_id, "state": state})

        responding: asyncio.Task | None = None
        speculative: asyncio.Future | None = None

        async def stream(make_events) -> None:
            """Run a blocking event generator in a worker thread and forward events as they arrive."""
            queue: asyncio.Queue = asyncio.Queue()

            def produce() -> None:
                try:
                    for event in make_events():
                        loop.call_soon_threadsafe(queue.put_nowait, event)
                except Exception as exc:  # surface failures instead of leaving the caller waiting
                    log.exception("call %s: response failed", session_id)
                    loop.call_soon_threadsafe(queue.put_nowait, {"type": "error", "message": str(exc)})
                finally:
                    loop.call_soon_threadsafe(queue.put_nowait, None)

            loop.run_in_executor(None, produce)
            while (event := await queue.get()) is not None:
                await ws.send_json(event)

        async def respond(audio, early: asyncio.Future | None) -> None:
            try:
                transcript = await early if early is not None else None
            except Exception:
                log.exception("call %s: speculative STT failed", session_id)
                transcript = None
            await stream(lambda: call.respond(audio, transcript))

        if greeting:
            await ws.send_json({"type": "agent_text", "text": greeting, "state": state})
            await stream(lambda: call.speak(greeting))

        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if message.get("text"):
                    data = json.loads(message["text"])
                    if data.get("type") == "hangup":
                        break
                    if data.get("type") == "playback":
                        call.agent_speaking = bool(data.get("playing"))
                    continue
                for event in call.feed(message.get("bytes") or b""):
                    kind = event["type"]
                    if kind == "user_speech_start":
                        if call.agent_speaking or (responding and not responding.done()):
                            call.cancel_speech()
                            await ws.send_json({"type": "barge_in"})
                        await ws.send_json(event)
                    elif kind == "_speculate":
                        # Start STT during the pause; reused if the caller really has finished.
                        speculative = loop.run_in_executor(None, call.transcribe, event["audio"])
                    elif kind == "_resume":
                        speculative = None
                    elif kind == "_turn":
                        responding = asyncio.create_task(respond(event["audio"], speculative))
                        speculative = None
        except WebSocketDisconnect:
            pass
        finally:
            if responding and not responding.done():
                call.cancel_speech()
                try:
                    await asyncio.wait_for(responding, timeout=10)
                except (asyncio.TimeoutError, WebSocketDisconnect, RuntimeError):
                    pass
            try:
                await ws.close()
            except RuntimeError:
                pass

    return app


_app = None

def __getattr__(name: str):
    """Lazy initialization of `app` for `uvicorn app.main:app`."""
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
